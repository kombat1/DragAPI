"""Агенты автоматизации и запуски сценариев.

Агент — исполнитель на конкретном компьютере. «Эта машина» (local) работает внутри сервера платформы
(если есть графический сеанс), удалённые подключаются по WebSocket /api/agents/ws с токеном.
Платформа решает, что и где запускать (вручную, по горячей клавише, по расписанию, по вебхуку),
агент исполняет план и присылает события. Шаги «Python-код» и «HTTP-запрос» агент просит выполнить
сервер — в песочнице и по правилам firewall проекта.

Права: локальным агентом (рабочим столом сервера) могут управлять только администраторы; удалённым —
администраторы и пользователи, которым его выдали. Горячие клавиши, расписание и вебхуки работают от
имени того, кто включил сценарий.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import secrets
import threading
import time
import uuid
import zipfile
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import auth, sandbox, storage, vault
from .automation import CompiledAutomation, compile_automation, recording_to_blocks
from .graph import GraphError
from .paths import DATA
from .runtime import apply_code_result

AGENTS_FILE = "agents.json"
LOCAL_ID = "local"
LOCAL_NAME = "Эта машина"
MAX_EVENTS = 2000
HISTORY = 30
WEEKDAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

HTTP_CODE = '''
import json, urllib.request, urllib.error

def run(data, env):
    req = urllib.request.Request(data["url"], method=data["method"],
                                 headers={"User-Agent": "DragAPI-scenario", "Accept": "application/json, */*"})
    body = None
    if data["method"] != "GET" and data["body"]:
        body = json.dumps(data["body"]).encode()
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, body, timeout=data["timeout"]) as r:
            status, raw = r.status, r.read(1_000_000)
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read(1_000_000)
    text = raw.decode("utf-8", "replace")
    out = {"status": status, "ok": 200 <= status < 300, "text": text[:100000], "json": None}
    try:
        out["json"] = json.loads(text)
    except ValueError:
        pass
    return {"response": out}
'''


def local_agent_wanted() -> bool:
    env = os.environ.get("DRAGAPI_LOCAL_AGENT", "").strip().lower()
    if env:
        return env not in ("off", "0", "false", "no")
    from . import config
    try:
        return config.load().get("local_agent", True) is not False
    except config.ConfigError:
        return True


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass
class Conn:
    id: str
    info: dict
    send: object                       # async (msg) -> None
    connected: float = field(default_factory=time.time)
    run_id: str | None = None


@dataclass
class Run:
    id: str
    pid: str
    scenario: str
    agent: str
    source: str
    user: str | None
    inputs: dict
    status: str = "running"            # starting | running | ok | error | stopped | skipped
    message: str = ""
    started: float = field(default_factory=time.time)
    finished: float | None = None
    events: list = field(default_factory=list)
    events_total: int = 0
    current: str | None = None
    vars: dict = field(default_factory=dict)
    actions: int = 0
    compiled: CompiledAutomation | None = None
    reply_chat: str | None = None      # запуск командой из Telegram — итог отправить туда
    tg: dict | None = None             # бот проекта: {token, chats, notify}
    done: asyncio.Event = field(default_factory=asyncio.Event)

    def as_dict(self, after: int | None = None) -> dict:
        out = {"id": self.id, "pid": self.pid, "scenario": self.scenario, "agent": self.agent,
               "source": self.source, "status": self.status, "message": self.message, "started": self.started,
               "finished": self.finished, "current": self.current, "vars": self.vars, "actions": self.actions,
               "events_total": self.events_total, "inputs": self.inputs}
        if after is not None:
            first = self.events_total - len(self.events)
            out["events"] = self.events[max(0, after - first):]
            out["events_from"] = max(after, first)
        return out


class AgentHub:
    def __init__(self, root: Path | None = None):
        self.root = root or DATA
        self.lock = threading.RLock()
        self.conns: dict[str, Conn] = {}
        self.runs: dict[str, Run] = {}
        self.history: dict[str, deque] = {}
        self.recordings: dict[str, dict] = {}
        self.points: dict[str, dict] = {}
        self.enabled: dict[str, CompiledAutomation] = {}   # pid -> включённые проекты (горячие клавиши, расписание)
        self.enabled_by: dict[str, str | None] = {}
        self.next_due: dict[tuple[str, str], float] = {}
        self.fired_daily: dict[tuple[str, str], str] = {}
        self.loop: asyncio.AbstractEventLoop | None = None
        self.local_agent = None
        self.local_error: str | None = None
        self.scheduler_task: asyncio.Task | None = None
        self.on_network = None   # (pid, events) -> None — журнал firewall
        from .telegram import TelegramManager
        self.telegram = TelegramManager(self)

    # ---------- реестр удалённых агентов ----------

    def _read(self) -> list[dict]:
        try:
            return json.loads((self.root / AGENTS_FILE).read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            return []

    def _write(self, items: list[dict]):
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / AGENTS_FILE
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=1)
        tmp.replace(path)

    def create_agent(self, name: str, user_id: str | None) -> tuple[dict, str]:
        name = (name or "").strip()[:60] or "Агент"
        token = "dga_" + secrets.token_urlsafe(32)
        rec = {"id": "agent-" + uuid.uuid4().hex[:8], "name": name, "token_hash": _hash(token),
               "created": time.time(), "created_by": user_id, "users": [user_id] if user_id else []}
        with self.lock:
            items = self._read()
            items.append(rec)
            self._write(items)
        return self.public(rec), token

    def update_agent(self, agent_id: str, name: str | None = None, users: list[str] | None = None) -> dict:
        with self.lock:
            items = self._read()
            rec = next((a for a in items if a["id"] == agent_id), None)
            if rec is None:
                raise KeyError(agent_id)
            if name is not None:
                rec["name"] = name.strip()[:60] or rec["name"]
            if users is not None:
                known = {u["id"] for u in auth.store.users()}
                rec["users"] = [u for u in users if u in known]
            self._write(items)
        return self.public(rec)

    async def delete_agent(self, agent_id: str):
        with self.lock:
            items = [a for a in self._read() if a["id"] != agent_id]
            self._write(items)
        conn = self.conns.get(agent_id)
        if conn is not None:
            await self._send(conn, {"type": "bye"})
            closer = conn.info.get("_close")
            if closer:
                await closer()

    def agent_by_token(self, token: str | None) -> dict | None:
        if not token:
            return None
        h = _hash(token)
        return next((a for a in self._read() if secrets.compare_digest(a["token_hash"], h)), None)

    def public(self, rec: dict) -> dict:
        return {k: rec.get(k) for k in ("id", "name", "created", "created_by", "users")}

    def can_use(self, user: dict | None, agent_id: str) -> bool:
        if not auth.enabled():
            return True
        if user is None:
            return False
        if user.get("role") == "admin":
            return True
        if agent_id == LOCAL_ID:
            return False
        rec = next((a for a in self._read() if a["id"] == agent_id), None)
        return bool(rec and user["id"] in (rec.get("users") or []))

    def can_use_id(self, user_id: str | None, agent_id: str) -> bool:
        if not auth.enabled():
            return True
        return self.can_use(auth.store.get_user(user_id) if user_id else None, agent_id)

    def listing(self, user: dict | None) -> list[dict]:
        out = []
        local_conn = self.conns.get(LOCAL_ID)
        if self.local_agent is not None or self.local_error:
            out.append({"id": LOCAL_ID, "name": LOCAL_NAME, "local": True, "online": local_conn is not None,
                        "error": self.local_error or (local_conn.info.get("error") if local_conn else None),
                        "info": self._info(local_conn), "busy": bool(local_conn and local_conn.run_id),
                        "can_use": self.can_use(user, LOCAL_ID)})
        for rec in self._read():
            conn = self.conns.get(rec["id"])
            out.append({**self.public(rec), "local": False, "online": conn is not None,
                        "error": conn.info.get("error") if conn else None, "info": self._info(conn),
                        "busy": bool(conn and conn.run_id), "can_use": self.can_use(user, rec["id"])})
        return out

    @staticmethod
    def _info(conn: Conn | None) -> dict | None:
        if conn is None:
            return None
        return {k: v for k, v in conn.info.items() if not k.startswith("_") and k != "type"} | \
            {"connected": conn.connected}

    # ---------- подключения ----------

    async def _send(self, conn: Conn, msg: dict):
        try:
            await conn.send(msg)
        except Exception:  # noqa: BLE001 — агент отвалился: разберётся detach
            pass

    async def attach(self, agent_id: str, info: dict, send) -> Conn:
        old = self.conns.get(agent_id)
        if old is not None:
            closer = old.info.get("_close")
            if closer:
                await closer()
            await self.detach(old)
        conn = Conn(agent_id, info, send)
        self.conns[agent_id] = conn
        await self.push_hotkeys(agent_id)
        return conn

    async def detach(self, conn: Conn):
        if self.conns.get(conn.id) is conn:
            del self.conns[conn.id]
        if conn.run_id and conn.run_id in self.runs:
            self._finish(self.runs[conn.run_id], {"status": "error", "message": "агент отключился во время запуска"})
        for rec in self.recordings.values():
            if rec["agent"] == conn.id and rec["state"] in ("starting", "countdown", "recording"):
                rec.update(state="error", message="агент отключился")
        for p in self.points.values():
            if p["agent"] == conn.id and not p.get("done") and "x" not in p and "error" not in p:
                p["error"] = "агент отключился"

    async def websocket(self, ws):
        """WS /api/agents/ws — подключение удалённого агента (Authorization: Bearer <токен>)."""
        header = ws.headers.get("authorization", "")
        token = header[7:].strip() if header.lower().startswith("bearer ") else None
        rec = self.agent_by_token(token)
        if rec is None:
            await ws.close(code=4401)
            return
        await ws.accept()
        try:
            hello = await asyncio.wait_for(ws.receive_json(), 15)
        except Exception:  # noqa: BLE001
            await ws.close(code=4400)
            return
        if not isinstance(hello, dict) or hello.get("type") != "hello":
            await ws.close(code=4400)
            return
        info = {k: hello.get(k) for k in ("name", "version", "platform", "screen", "stop_hotkey", "error",
                                           "capabilities")}
        lock = asyncio.Lock()

        async def send(msg):
            async with lock:
                await ws.send_json(msg)

        async def close():
            try:
                await ws.close(code=4000)
            except Exception:  # noqa: BLE001
                pass

        info["_close"] = close
        conn = await self.attach(rec["id"], info, send)
        try:
            while True:
                msg = await ws.receive_json()
                if isinstance(msg, dict):
                    await self.on_message(conn, msg)
        except Exception:  # noqa: BLE001 — разрыв соединения
            pass
        finally:
            await self.detach(conn)

    # ---------- локальный агент ----------

    async def start_local(self, loop: asyncio.AbstractEventLoop):
        if not local_agent_wanted():
            return
        try:
            from dragapi_agent.agent import Agent
        except Exception as e:  # noqa: BLE001
            self.local_error = f"агент недоступен: {e}"
            return

        def from_agent(msg):
            loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self._local_message(msg)))

        agent = Agent(from_agent, name=LOCAL_NAME)
        hello = await asyncio.to_thread(agent.hello)
        if hello.get("error"):
            self.local_error = hello["error"]
            return
        await asyncio.to_thread(agent.start)
        self.local_agent = agent

        async def send(msg):
            await asyncio.to_thread(agent.handle, msg)

        info = {k: hello.get(k) for k in ("name", "version", "platform", "screen", "stop_hotkey", "error",
                                           "capabilities")}
        await self.attach(LOCAL_ID, info, send)

    async def _local_message(self, msg):
        conn = self.conns.get(LOCAL_ID)
        if conn is not None:
            await self.on_message(conn, msg)

    def stop_local(self):
        if self.local_agent is not None:
            self.local_agent.shutdown()
            self.local_agent = None

    # ---------- сообщения агента ----------

    async def on_message(self, conn: Conn, msg: dict):
        kind = msg.get("type")
        run = self.runs.get(msg.get("run_id")) if msg.get("run_id") else None
        if run is not None and run.agent != conn.id:
            return
        if kind == "event" and run is not None:
            event = msg.get("event") if isinstance(msg.get("event"), dict) else {}
            if event.get("node"):
                run.current = event["node"]
            run.events.append(event)
            run.events_total += 1
            if len(run.events) > MAX_EVENTS:
                del run.events[: len(run.events) - MAX_EVENTS]
        elif kind == "done" and run is not None:
            self._finish(run, msg)
        elif kind == "op" and run is not None:
            asyncio.ensure_future(self._server_op(conn, run, msg))
        elif kind == "hotkey":
            await self._hotkey(conn, msg.get("pid"), msg.get("trigger"))
        elif kind == "record_state":
            rec = self.recordings.get(msg.get("rec_id"))
            if rec and rec["agent"] == conn.id:
                rec.update(state=msg.get("state"), left=msg.get("left"), message=msg.get("message"))
        elif kind == "recorded":
            rec = self.recordings.get(msg.get("rec_id"))
            if rec and rec["agent"] == conn.id:
                events = msg.get("events") or []
                # остановили кнопкой в браузере: если браузер на том же экране, последний щелчок — по этой
                # кнопке. Отбрасываем его, только если он был прямо перед остановкой (агент может быть другим ПК)
                downs = [e.get("t", 0) for e in events if e.get("kind") == "down"]
                stopped_at = float(msg.get("stopped_at") or 0)
                drop = bool(rec.get("stop_from_ui") and not msg.get("by_user") and downs
                            and stopped_at - downs[-1] <= 1.5)
                rec.update(state="done", events=len(events), blocks=recording_to_blocks(events, drop_last_click=drop))
        elif kind in ("point", "shot", "vision_result", "windows"):
            p = self.points.get(str(msg.get("req")))
            if p and p["agent"] == conn.id:
                p.update({k: msg[k] for k in ("x", "y", "left", "error", "data", "w", "h", "ox", "oy", "status", "message", "vars",
                                                  "window", "windows", "items")
                          if k in msg})
                if kind in ("vision_result", "windows") or (kind == "shot" and not msg.get("left")):
                    p["done"] = True

    def _finish(self, run: Run, msg: dict):
        if run.finished is not None:
            return
        run.status = msg.get("status") or "error"
        run.message = str(msg.get("message") or "")
        run.vars = msg.get("vars") or run.vars
        run.actions = msg.get("actions") or 0
        run.finished = time.time()
        run.compiled = None
        conn = self.conns.get(run.agent)
        if conn is not None and conn.run_id == run.id:
            conn.run_id = None
        run.done.set()
        self.telegram.cancel_run(run.id)
        self._notify(run)

    def _notify(self, run: Run):
        if run.tg and (run.reply_chat or run.tg["notify"] != "never"):
            self._spawn(self.telegram.notify_finished(run, run.tg["token"], run.tg["chats"], run.tg["notify"]))

    async def _server_op(self, conn: Conn, run: Run, msg: dict):
        req = msg.get("req")
        try:
            result = await self.server_op(run, msg.get("op"), msg.get("node"), msg.get("payload") or {})
            reply = {"type": "op_result", "req": req, "ok": True, "result": result}
        except Exception as e:  # noqa: BLE001
            reply = {"type": "op_result", "req": req, "ok": False, "error": str(e)}
        await self._send(conn, reply)

    async def server_op(self, run: Run, op: str, node: str, payload: dict) -> dict:
        project = run.compiled
        if project is None:
            raise RuntimeError("запуск уже завершён")
        scenario = project.scenario(run.scenario)
        from .pyenv import envs
        if op == "code":
            spec = scenario.code.get(node) if scenario else None
            if spec is None:
                raise RuntimeError("нет такого блока кода в сценарии")
            r = await sandbox.run_code(spec["code"], payload.get("data") or {}, project.env_values,
                                       sandbox.Limits(spec["timeout"], spec["memory_mb"]), project.sandbox,
                                       envs.venv(run.pid), project.firewall)
            self._network(run.pid, r.network)
            if r.stdout:
                run.events.append({"type": "log", "node": node, "message": r.stdout[-2000:], "t": time.time()})
                run.events_total += 1
            if not r.ok:
                raise RuntimeError(f"Python-код: {r.error}")
            return apply_code_result(r.result, spec["outputs"])
        if op in ("tg_send", "tg_ask"):
            step = _find_step(scenario.steps if scenario else [], node) or next(
                (st for sub in (scenario.subs.values() if scenario else []) if (st := _find_step(sub["steps"], node))), None)
            if step is None:
                raise RuntimeError("нет такого Telegram-блока в сценарии")
            token = project.telegram_token
            if not token:
                raise RuntimeError(f"не задан токен бота (переменная {project.telegram.token_env or '—'})")
            chats = [step["chat"]] if step.get("chat") else list(project.telegram.chats)
            from .telegram import TelegramError
            try:
                if op == "tg_send":
                    sent = await self.telegram.send_many(token, chats, str(payload.get("text") or ""),
                                                         bool(step.get("silent")))
                    return {"sent": sent}
                timeout = min(float(payload.get("timeout") or 300), 86400)
                return await self.telegram.ask(token, chats, str(payload.get("text") or ""), step.get("yes") or "Да",
                                               step.get("no") or "Нет", timeout, run.id)
            except TelegramError as e:
                raise RuntimeError(f"Telegram: {e}") from None
        if op == "http":
            spec = scenario.http.get(node) if scenario else None
            if spec is None:
                raise RuntimeError("нет такого HTTP-блока в сценарии")
            mode = sandbox.egress_mode(project.sandbox, project.firewall)
            if mode == "off":
                raise RuntimeError("сеть для проекта закрыта — разрешите адрес в «Настройки проекта → Firewall»")
            url = str(payload.get("url") or "")
            if not url.startswith(("http://", "https://")):
                raise RuntimeError(f"адрес должен начинаться с http:// или https://: {url[:100]}")
            data = {"url": url, "method": spec["method"], "body": payload.get("body") or {},
                    "timeout": spec["timeout"]}
            r = await sandbox.run_code(HTTP_CODE, data, {}, sandbox.Limits(min(spec["timeout"] + 2, 10), 128),
                                       project.sandbox, None, project.firewall)
            self._network(run.pid, r.network)
            if not r.ok:
                raise RuntimeError(f"HTTP-запрос: {r.error}")
            return (r.result or {}).get("response")
        raise RuntimeError(f"неизвестная операция «{op}»")

    def _network(self, pid, events):
        if events and self.on_network:
            self.on_network(pid, [{**e, "source": "сценарий"} for e in events])

    # ---------- запуски ----------

    def project_history(self, pid: str) -> deque:
        return self.history.setdefault(pid, deque(maxlen=HISTORY))

    async def start_run(self, pid: str, compiled: CompiledAutomation, name: str, inputs: dict, source: str,
                        user_id: str | None, agent_id: str | None = None, reply_chat: str | None = None) -> Run:
        """Запустить сценарий. Ошибки «нельзя» — ValueError (сообщение для пользователя)."""
        scenario = compiled.scenario(name)
        if scenario is None:
            raise LookupError(f"сценария «{name}» нет (или в нём ошибки)")
        agent_id = agent_id or compiled.settings.agent or LOCAL_ID
        run = Run("run-" + uuid.uuid4().hex[:10], pid, name, agent_id, source, user_id,
                  {k: v for k, v in inputs.items() if k in scenario.trigger.params})
        if compiled.telegram.configured and compiled.telegram_token:
            run.tg = {"token": compiled.telegram_token, "chats": list(compiled.telegram.chats),
                      "notify": compiled.telegram.notify}
        history = self.project_history(pid)
        conn = self.conns.get(agent_id)
        problem = None
        if not self.can_use_id(user_id, agent_id):
            problem = "нет прав на этого агента"
        elif conn is None:
            problem = "агент не в сети" if agent_id != LOCAL_ID else \
                f"локальный агент недоступен{': ' + self.local_error if self.local_error else ''}"
        elif conn.run_id is not None:
            problem = "агент занят другим сценарием"
        elif scenario.needs_new_agent and "retry" not in ((conn.info or {}).get("capabilities") or []):
            problem = ("агент устарел: повторы шагов, ветку «ошибка» и подпрограммы он не выполнит — "
                       "скачайте агента заново («🖥 Агенты» → zip) и перезапустите его")
        missing = [p for p in scenario.trigger.params if p not in inputs]
        if problem is None and missing:
            problem = "не переданы параметры: " + ", ".join(missing)
        if problem:
            run.status, run.message, run.finished = "skipped", problem, time.time()
            run.done.set()
            history.appendleft(run)
            if source in ("hotkey", "schedule", "webhook"):   # ручной и telegram-запуск сообщают сами
                self._notify(run)
            raise ValueError(problem)
        run.reply_chat = reply_chat
        run.compiled = compiled
        conn.run_id = run.id
        self.runs[run.id] = run
        history.appendleft(run)
        await self._send(conn, {"type": "run", "run_id": run.id, "scenario": scenario.plan(),
                                "env": compiled.env_values, "inputs": run.inputs,
                                "limits": compiled.settings.limits()})
        return run

    async def stop_run(self, run_id: str):
        run = self.runs.get(run_id)
        if run is None or run.finished:
            return
        conn = self.conns.get(run.agent)
        if conn is not None:
            await self._send(conn, {"type": "stop", "run_id": run_id})
        else:
            self._finish(run, {"status": "stopped", "message": "агент недоступен"})

    def get_run(self, pid: str, run_id: str) -> Run | None:
        run = self.runs.get(run_id)
        if run is None:
            run = next((r for r in self.project_history(pid) if r.id == run_id), None)
        return run if run is not None and run.pid == pid else None

    # ---------- включённые сценарии: горячие клавиши, расписание ----------

    def refresh_project(self, pid: str, project: dict | None):
        """После сохранения или удаления проекта: обновить триггеры."""
        compiled = None
        if project and project.get("kind") == "automation" and (project.get("automation") or {}).get("enabled"):
            try:
                compiled = compile_automation(self._unsealed(pid, project))
            except (GraphError, vault.VaultError):
                compiled = None
        old_agent = self.enabled[pid].settings.agent if pid in self.enabled else None
        if compiled is not None:
            self.enabled[pid] = compiled
            self.enabled_by[pid] = (project.get("automation") or {}).get("enabled_by")
        else:
            self.enabled.pop(pid, None)
            self.enabled_by.pop(pid, None)
        for key in [k for k in self.next_due if k[0] == pid]:
            del self.next_due[key]
        for agent_id in {old_agent, compiled.settings.agent if compiled else None} - {None}:
            self._spawn(self.push_hotkeys(agent_id))
        if self.loop is not None:
            self.telegram.sync()

    def _spawn(self, coro):
        """Запустить корутину в цикле сервера — и из него самого, и из потока (синхронные эндпоинты)."""
        if self.loop is None:
            coro.close()
        elif _in_loop(self.loop):
            asyncio.ensure_future(coro)
        else:
            asyncio.run_coroutine_threadsafe(coro, self.loop)

    @staticmethod
    def _unsealed(pid: str, project: dict) -> dict:
        return {**project, "env": vault.unseal_env(project.get("env"), project.get("env"))}

    def load_enabled(self):
        for p in storage.list_projects():
            if p.get("kind") == "automation":
                try:
                    self.refresh_project(p["id"], storage.load(p["id"]))
                except storage.NotFound:
                    pass

    async def push_hotkeys(self, agent_id: str):
        conn = self.conns.get(agent_id)
        if conn is None:
            return
        items = []
        for pid, compiled in self.enabled.items():
            if compiled.settings.agent != agent_id or not self.can_use_id(self.enabled_by.get(pid), agent_id):
                continue
            for sc in compiled.scenarios:
                if sc.trigger.kind == "hotkey" and sc.trigger.hotkey:
                    items.append({"keys": "+".join(sc.trigger.hotkey), "pid": pid, "trigger": sc.trigger.name})
        await self._send(conn, {"type": "hotkeys", "items": items})

    async def _hotkey(self, conn: Conn, pid, name):
        compiled = self.enabled.get(pid)
        if compiled is None or compiled.settings.agent != conn.id:
            return
        sc = compiled.scenario(name)
        if sc is None or sc.trigger.kind != "hotkey":
            return
        try:
            await self.start_run(pid, compiled, name, {}, "hotkey", self.enabled_by.get(pid))
        except (ValueError, LookupError):
            pass  # причина — в истории запусков

    async def scheduler(self):
        while True:
            try:
                await self.tick(time.time())
            except Exception as e:  # noqa: BLE001 — планировщик не должен умирать
                print(f"DragAPI: ошибка планировщика: {e}")
            await asyncio.sleep(1)

    async def tick(self, now: float):
        local = datetime.fromtimestamp(now)
        today = local.strftime("%Y-%m-%d")
        hhmm = local.strftime("%H:%M")
        weekday = WEEKDAY_KEYS[local.weekday()]
        for pid, compiled in list(self.enabled.items()):
            for sc in compiled.scenarios:
                t = sc.trigger
                key = (pid, t.name)
                due = False
                if t.kind == "interval":
                    nxt = self.next_due.get(key)
                    if nxt is None:
                        self.next_due[key] = now + t.every_seconds
                    elif now >= nxt:
                        self.next_due[key] = now + t.every_seconds
                        due = True
                elif t.kind == "daily":
                    if hhmm == t.at and (not t.weekdays or weekday in t.weekdays) and \
                            self.fired_daily.get(key) != today:
                        self.fired_daily[key] = today
                        due = True
                if due:
                    try:
                        await self.start_run(pid, compiled, t.name, {}, "schedule", self.enabled_by.get(pid))
                    except (ValueError, LookupError):
                        pass

    # ---------- запись и позиция курсора ----------

    async def record_start(self, agent_id: str, countdown: int) -> dict:
        conn = self.conns.get(agent_id)
        if conn is None:
            raise ValueError("агент не в сети")
        if conn.run_id:
            raise ValueError("агент занят сценарием")
        rec_id = "rec-" + uuid.uuid4().hex[:10]
        rec = {"id": rec_id, "agent": agent_id, "state": "starting", "started": time.time()}
        self.recordings[rec_id] = rec
        await self._send(conn, {"type": "record_start", "rec_id": rec_id, "countdown": countdown})
        return rec

    async def record_stop(self, rec_id: str):
        rec = self.recordings.get(rec_id)
        if rec is None:
            return
        rec["stop_from_ui"] = True
        conn = self.conns.get(rec["agent"])
        if conn is not None:
            await self._send(conn, {"type": "record_stop", "rec_id": rec_id})

    async def ask_agent(self, agent_id: str, msg: dict, *, need: str | None = None, **state) -> dict:
        """Короткий запрос к агенту (позиция курсора, снимок экрана, проверка блока): ответ — в self.points."""
        conn = self.conns.get(agent_id)
        if conn is None:
            raise ValueError("агент не в сети")
        if need and need not in ((conn.info or {}).get("capabilities") or []):
            raise ValueError("агент устарел и не умеет этого — скачайте и запустите агента заново")
        req = uuid.uuid4().hex[:10]
        self.points[req] = {"id": req, "agent": agent_id, **state}
        if len(self.points) > 100:
            for old in list(self.points)[:-100]:
                del self.points[old]
        await self._send(conn, {**msg, "req": req})
        return self.points[req]

    async def pick_point(self, agent_id: str, countdown: int) -> dict:
        return await self.ask_agent(agent_id, {"type": "pick_point", "countdown": countdown}, left=countdown)


def _find_step(steps: list[dict], node: str) -> dict | None:
    for step in steps:
        if step.get("node") == node:
            return step
        for key in ("then", "else", "body", "next", "on_fail"):
            found = _find_step(step.get(key) or [], node)
            if found:
                return found
    return None


def _in_loop(loop) -> bool:
    try:
        return asyncio.get_running_loop() is loop
    except RuntimeError:
        return False


def agent_bundle() -> bytes:
    """zip с удалённым агентом: пакет dragapi_agent + вычислитель выражений."""
    root = Path(__file__).resolve().parent.parent
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted((root / "dragapi_agent").glob("*.py")):
            z.write(f, f"dragapi_agent/{f.name}")
        z.write(root / "app" / "expr.py", "dragapi_agent/expr.py")
        z.writestr("requirements.txt", "pynput>=1.7\nwebsockets>=12\n"
                   "# блоки «Зрение» (поиск картинки, цвет пикселя, OCR):\nnumpy>=1.26\nopencv-python-headless>=4.8\nmss>=9\n")
        z.writestr("README.txt", (
            "Агент автоматизации DragAPI\n\n"
            "1. Python 3.10+:  python -m venv .venv && .venv/bin/pip install -r requirements.txt\n"
            "   (Windows: .venv\\Scripts\\pip install -r requirements.txt)\n"
            "2. Запуск:  DRAGAPI_AGENT_TOKEN=<токен> .venv/bin/python -m dragapi_agent --server http://<платформа>:8765\n"
            "   (платформа по https — --server https://…; её собственный сертификат лежит здесь же в\n"
            "   platform-ca.pem, агент подхватывает его сам; другой файл — --ca <файл>)\n\n"
            "Linux: нужен сеанс X11 (на Wayland управление вводом ограничено).\n"
            "Распознавание текста (блок «Прочитать текст»): программа tesseract —\n"
            "   Linux: sudo apt install tesseract-ocr tesseract-ocr-rus;  Windows: установщик UB Mannheim,\n"
            "   путь к tesseract.exe — в переменной DRAGAPI_TESSERACT, если его нет в PATH.\n"
            "Аварийная остановка сценария: Ctrl+Alt+Esc или курсор в левый верхний угол экрана.\n"))
        from .tls import active_ca
        ca = active_ca()
        if ca:  # платформа на своём сертификате — агент доверяет ему по этому файлу (подхватывается сам)
            z.write(ca, "platform-ca.pem")
    return buf.getvalue()


hub = AgentHub()
