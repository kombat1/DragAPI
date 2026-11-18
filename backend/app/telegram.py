"""Telegram-боты проектов-автоматизаций.

Бот — токен в секретной переменной проекта (настройки → Telegram). Платформа сама опрашивает Telegram
(long polling getUpdates), поэтому публичный адрес и открытые порты не нужны. Один опросчик на токен,
даже если бот общий у нескольких проектов.

Команды принимаются только из разрешённых чатов проекта (белый список): бот управляет компьютером.
    /<команда> [параметры]  запустить сценарий со «Стартом» «Команда Telegram-бота»
    /list (/help, /start)   список команд
    /status                 что выполняется
    /stop                   остановить запуски
    /id                     ID этого чата (отвечает и в неразрешённых чатах — чтобы добавить его)
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field

import httpx

API_BASE = os.environ.get("DRAGAPI_TELEGRAM_API", "https://api.telegram.org")
TRANSPORT = None   # тесты подменяют на httpx.MockTransport
POLL_TIMEOUT = 25


class TelegramError(Exception):
    pass


async def call(token: str, method: str, http_timeout: float = 15, **params):
    """Вызов Bot API (params — параметры метода, в т.ч. timeout у getUpdates). Ошибка — TelegramError."""
    if not token:
        raise TelegramError("не задан токен бота")
    url = f"{API_BASE}/bot{token}/{method}"
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=http_timeout + 10, transport=TRANSPORT) as client:
                r = await client.post(url, json={k: v for k, v in params.items() if v is not None})
        except httpx.HTTPError as e:
            raise TelegramError(f"Telegram недоступен: {type(e).__name__}") from None
        try:
            data = r.json()
        except ValueError:
            raise TelegramError(f"Telegram ответил {r.status_code}") from None
        if data.get("ok"):
            return data.get("result")
        retry = (data.get("parameters") or {}).get("retry_after")
        if r.status_code == 429 and retry and attempt == 0 and retry <= 10:
            await asyncio.sleep(retry)
            continue
        desc = data.get("description") or f"ошибка {r.status_code}"
        if r.status_code == 401:
            desc = "токен бота неверный (Unauthorized)"
        elif r.status_code == 409:
            desc = "бота уже опрашивает другая программа или у него настроен webhook (Conflict)"
        raise TelegramError(desc)
    raise TelegramError("Telegram: слишком много запросов")


def parse_command(text: str) -> tuple[str, str] | None:
    """«/Click@MyBot n=5» → ("click", "n=5")."""
    text = (text or "").strip()
    if not text.startswith("/"):
        return None
    head, _, rest = text[1:].partition(" ")
    return head.split("@", 1)[0].lower(), rest.strip()


def parse_args(rest: str, params: list[str]) -> dict:
    """Параметры команды: «a=1 b=текст» или позиционно в порядке параметров «Старта»;
    при одном параметре — вся строка целиком. Значения — JSON, если разбираются (5, true, [1]), иначе строки."""
    def value(v: str):
        try:
            return json.loads(v)
        except ValueError:
            return v
    rest = rest.strip()
    if not rest:
        return {}
    tokens = rest.split()
    if all("=" in t for t in tokens):
        return {k: value(v) for k, _, v in (t.partition("=") for t in tokens)}
    if len(params) == 1:
        return {params[0]: value(rest)}
    return {p: value(t) for p, t in zip(params, tokens)}


def keyboard(yes: str, no: str, key: str) -> dict:
    return {"inline_keyboard": [[{"text": yes[:60], "callback_data": f"{key}:1"},
                                 {"text": no[:60], "callback_data": f"{key}:0"}]]}


@dataclass
class Poller:
    token: str
    task: asyncio.Task | None = None
    offset: int = 0
    username: str = ""
    error: str | None = None
    started: float = field(default_factory=time.time)
    last_update: float | None = None
    seen_chats: dict = field(default_factory=dict)   # id -> название: подсказка в настройках
    refs: int = 0                                    # временные пользователи (вопрос в выключенном проекте)


class TelegramManager:
    def __init__(self, hub):
        self.hub = hub
        self.pollers: dict[str, Poller] = {}
        self.questions: dict[str, dict] = {}         # key -> {future, chats, run_id, messages}

    # ---------- какие боты нужны ----------

    def routes(self, token: str) -> list[tuple[str, object]]:
        """Включённые проекты с этим ботом: [(pid, compiled)]."""
        return [(pid, c) for pid, c in self.hub.enabled.items()
                if c.telegram.configured and c.telegram_token == token]

    def sync(self):
        """После изменения включённых проектов: запустить нужные опросчики, остановить лишние."""
        wanted = {c.telegram_token for c in self.hub.enabled.values() if c.telegram.configured and c.telegram_token}
        from .bots import manager as bot_manager
        busy = {t for t in wanted if bot_manager.uses_token(t)}   # токен занят ботом из конструктора
        wanted -= busy
        for token in wanted:
            self._ensure(token)
        for token, poller in list(self.pollers.items()):
            if token not in wanted and poller.refs <= 0:
                self._stop(token)
        for token in wanted:
            self.hub._spawn(self._set_commands(token))

    def _ensure(self, token: str) -> Poller:
        poller = self.pollers.get(token)
        if poller is None:
            poller = Poller(token)
            self.pollers[token] = poller
        if (poller.task is None or poller.task.done()) and self.hub.loop is not None:
            def start():
                poller.task = asyncio.ensure_future(self._poll(poller))
            if _in_loop(self.hub.loop):
                start()
            else:
                self.hub.loop.call_soon_threadsafe(start)
        return poller

    def _stop(self, token: str):
        poller = self.pollers.pop(token, None)
        if poller and poller.task and not poller.task.done():
            self.hub.loop.call_soon_threadsafe(poller.task.cancel)

    def stop_all(self):
        for token in list(self.pollers):
            poller = self.pollers.pop(token)
            if poller.task and not poller.task.done():
                poller.task.cancel()

    def status(self, token: str) -> dict | None:
        poller = self.pollers.get(token)
        if poller is None:
            return None
        return {"running": bool(poller.task and not poller.task.done()), "username": poller.username,
                "error": poller.error, "last_update": poller.last_update,
                "seen_chats": [{"id": k, "title": v} for k, v in poller.seen_chats.items()]}

    async def _set_commands(self, token: str):
        commands = [{"command": "list", "description": "Список сценариев"},
                    {"command": "status", "description": "Что выполняется"},
                    {"command": "stop", "description": "Остановить запуски"},
                    {"command": "id", "description": "ID этого чата"}]
        for _pid, compiled in self.routes(token):
            for cmd, sc in compiled.telegram_commands().items():
                desc = f"Сценарий «{sc.trigger.name}»" + (f" ({', '.join(sc.trigger.params)})" if sc.trigger.params else "")
                commands.append({"command": cmd, "description": desc[:250]})
        try:
            await call(token, "setMyCommands", commands=commands[:100])
        except TelegramError:
            pass

    # ---------- опрос ----------

    async def _poll(self, poller: Poller):
        backoff = 1
        try:
            me = await call(poller.token, "getMe")
            poller.username = me.get("username") or ""
        except TelegramError as e:
            poller.error = str(e)
        while True:
            began = time.monotonic()
            try:
                # long polling: Telegram держит запрос до POLL_TIMEOUT секунд, пока нет сообщений
                updates = await call(poller.token, "getUpdates", http_timeout=POLL_TIMEOUT, timeout=POLL_TIMEOUT,
                                     offset=poller.offset or None, allowed_updates=["message", "callback_query"])
                poller.error = None
                backoff = 1
            except asyncio.CancelledError:
                raise
            except TelegramError as e:
                poller.error = str(e)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            for upd in updates or []:
                poller.offset = max(poller.offset, int(upd.get("update_id", 0)) + 1)
                poller.last_update = time.time()
                try:
                    await self._update(poller, upd)
                except Exception as e:  # noqa: BLE001 — одно кривое сообщение не должно ронять бота
                    print(f"DragAPI: Telegram: ошибка обработки сообщения: {e}")
            if not updates and time.monotonic() - began < 0.5:
                await asyncio.sleep(1)   # защита от холостого цикла, если сервер не держит long polling

    async def _update(self, poller: Poller, upd: dict):
        if "callback_query" in upd:
            await self._callback(poller, upd["callback_query"])
            return
        msg = upd.get("message") or {}
        chat = msg.get("chat") or {}
        chat_id = str(chat.get("id", ""))
        if not chat_id:
            return
        poller.seen_chats[chat_id] = chat.get("title") or " ".join(
            x for x in (chat.get("first_name"), chat.get("last_name")) if x) or chat.get("username") or chat_id
        parsed = parse_command(msg.get("text") or "")
        if parsed is None:
            return
        command, rest = parsed
        routes = [(pid, c) for pid, c in self.routes(poller.token) if chat_id in c.telegram.chats]
        if command == "id" or not routes:
            text = f"ID этого чата: {chat_id}"
            if not routes:
                text += "\nЧат не в списке разрешённых — добавьте ID в «Настройки проекта → Telegram»."
            await self.send(poller.token, chat_id, text)
            return
        if command in ("start", "help", "list"):
            lines = []
            for pid, c in routes:
                for cmd, sc in c.telegram_commands().items():
                    params = " ".join(f"<{p}>" for p in sc.trigger.params)
                    lines.append(f"/{cmd} {params}".rstrip() + f" — «{sc.trigger.name}» ({c.name or pid})")
            await self.send(poller.token, chat_id, ("Команды:\n" + "\n".join(lines) if lines else
                                                    "Сценариев с запуском из Telegram нет.") +
                            "\n\n/status — что выполняется, /stop — остановить, /id — ID чата")
            return
        pids = {pid for pid, _ in routes}
        active = [r for r in self.hub.runs.values() if r.pid in pids and r.finished is None]
        if command == "status":
            await self.send(poller.token, chat_id, "\n".join(
                f"● «{r.scenario}» — {time.time() - r.started:.0f} с, действий {r.actions}" for r in active)
                or "Сейчас ничего не выполняется.")
            return
        if command == "stop":
            for r in active:
                await self.hub.stop_run(r.id)
            await self.send(poller.token, chat_id, f"Остановлено запусков: {len(active)}" if active
                            else "Сейчас ничего не выполняется.")
            return
        targets = [(pid, c, c.telegram_commands()[command]) for pid, c in routes if command in c.telegram_commands()]
        if not targets:
            await self.send(poller.token, chat_id, f"Неизвестная команда /{command}. Список — /list")
            return
        if len(targets) > 1:
            await self.send(poller.token, chat_id, f"/{command} есть в нескольких проектах — переименуйте команду")
            return
        pid, compiled, sc = targets[0]
        inputs = parse_args(rest, sc.trigger.params)
        try:
            run = await self.hub.start_run(pid, compiled, sc.trigger.name, inputs, "telegram",
                                           self.hub.enabled_by.get(pid), reply_chat=chat_id)
        except (ValueError, LookupError) as e:
            await self.send(poller.token, chat_id, f"Не запущено: {e}")
            return
        await self.send(poller.token, chat_id, f"▶ Запускаю «{sc.trigger.name}»…", silent=True)
        _ = run

    async def _callback(self, poller: Poller, cq: dict):
        data = str(cq.get("data") or "")
        key, _, answer = data.rpartition(":")
        q = self.questions.get(key)
        chat_id = str(((cq.get("message") or {}).get("chat") or {}).get("id", ""))
        who = (cq.get("from") or {}).get("first_name") or (cq.get("from") or {}).get("username") or ""
        if q is None or q["future"].done():
            await self._answer_cb(poller.token, cq.get("id"), "Вопрос уже закрыт")
            return
        if chat_id not in q["chats"]:
            await self._answer_cb(poller.token, cq.get("id"), "Этот чат не может отвечать")
            return
        q["future"].set_result({"answer": answer == "1", "by": who})
        await self._answer_cb(poller.token, cq.get("id"), "Принято")

    async def _answer_cb(self, token, cq_id, text):
        try:
            await call(token, "answerCallbackQuery", callback_query_id=cq_id, text=text)
        except TelegramError:
            pass

    # ---------- отправка ----------

    async def send(self, token: str, chat_id: str, text: str, silent: bool = False, markup: dict | None = None):
        return await call(token, "sendMessage", chat_id=chat_id, text=text[:4096],
                          disable_notification=silent or None, reply_markup=markup)

    async def send_many(self, token: str, chats: list[str], text: str, silent: bool = False) -> int:
        if not chats:
            raise TelegramError("нет чатов для отправки")
        errors, sent = [], 0
        for chat in chats:
            try:
                await self.send(token, chat, text, silent)
                sent += 1
            except TelegramError as e:
                errors.append(f"{chat}: {e}")
        if not sent:
            raise TelegramError("; ".join(errors))
        return sent

    async def ask(self, token: str, chats: list[str], text: str, yes: str, no: str, timeout: float,
                  run_id: str) -> dict:
        """Сообщение с кнопками; ждём первого ответа из разрешённых чатов. Нет ответа — answer=None."""
        key = os.urandom(6).hex()
        future = asyncio.get_running_loop().create_future()
        q = {"future": future, "chats": set(chats), "run_id": run_id, "messages": []}
        self.questions[key] = q
        poller = self._ensure(token)   # ответы приходят через опрос — даже если проект выключен
        poller.refs += 1
        try:
            for chat in chats:
                try:
                    m = await self.send(token, chat, text, markup=keyboard(yes, no, key))
                    q["messages"].append((chat, m.get("message_id")))
                except TelegramError as e:
                    if len(chats) == 1:
                        raise
                    print(f"DragAPI: Telegram: не отправлено в {chat}: {e}")
            if not q["messages"]:
                raise TelegramError("вопрос не удалось отправить ни в один чат")
            try:
                result = await asyncio.wait_for(asyncio.shield(future), timeout)
            except asyncio.TimeoutError:
                result = {"answer": None}
            # убрать кнопки и показать итог
            verdict = ("⌛ без ответа" if result["answer"] is None else
                       f"✔ {yes if result['answer'] else no}" + (f" — {result.get('by')}" if result.get("by") else ""))
            for chat, mid in q["messages"]:
                try:
                    await call(token, "editMessageText", chat_id=chat, message_id=mid, text=f"{text}\n\n{verdict}"[:4096])
                except TelegramError:
                    pass
            return result
        finally:
            self.questions.pop(key, None)
            poller.refs -= 1
            if poller.refs <= 0 and poller.token not in {c.telegram_token for c in self.hub.enabled.values()}:
                self._stop(poller.token)

    def cancel_run(self, run_id: str):
        for q in self.questions.values():
            if q["run_id"] == run_id and not q["future"].done():
                q["future"].set_result({"answer": None})

    async def notify_finished(self, run, token: str, chats: list[str], notify: str):
        """Итог запуска: ответ в чат, откуда пришла команда, и уведомление по настройке проекта."""
        icon = {"ok": "✅", "error": "❌", "stopped": "■", "skipped": "⚠"}.get(run.status, "•")
        took = f" за {run.finished - run.started:.1f} с" if run.finished else ""
        text = f"{icon} «{run.scenario}»: {_STATUS.get(run.status, run.status)}{took}"
        if run.message:
            text += f"\n{run.message}"
        shown = {k: v for k, v in list((run.vars or {}).items())[:10]}
        if shown and run.status == "ok":
            text += "\n" + "\n".join(f"{k} = {json.dumps(v, ensure_ascii=False)[:200]}" for k, v in shown.items())
        targets = []
        if run.reply_chat:
            targets.append(run.reply_chat)
        if notify == "always" or (notify == "errors" and run.status in ("error", "skipped")):
            targets += [c for c in chats if c not in targets]
        for chat in targets:
            try:
                await self.send(token, chat, text, silent=run.status == "ok")
            except TelegramError as e:
                print(f"DragAPI: Telegram: уведомление не отправлено: {e}")


_STATUS = {"ok": "готово", "error": "ошибка", "stopped": "остановлен", "skipped": "не запущен"}


def _in_loop(loop) -> bool:
    try:
        return asyncio.get_running_loop() is loop
    except RuntimeError:
        return False
