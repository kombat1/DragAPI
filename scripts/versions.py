#!/usr/bin/env python3
"""История версий DragAPI: коммиты с датой и описанием «что добавлено / что исправлено» и откат к любому.

    python3 scripts/versions.py                 меню: список → номер версии → посмотреть / откатиться
    python3 scripts/versions.py list [-n 30]    список версий
    python3 scripts/versions.py show 3          подробно: добавлено, исправлено, изменённые файлы
    python3 scripts/versions.py try 3           посмотреть версию в отдельной ветке (ваша ветка не меняется)
    python3 scripts/versions.py back            вернуться из просмотра в свою ветку
    python3 scripts/versions.py rollback 3      откатить текущую ветку к версии (с резервной копией)
    python3 scripts/versions.py undo            отменить последний откат
    python3 scripts/versions.py backups         резервные копии

Версию можно указать номером из списка (1 — самая новая) или хешем коммита.
Скрипт работает только с локальным репозиторием: ничего не отправляет и не удаляет на GitHub.
Незакоммиченные изменения не теряются: откат без --stash откажется, с --stash — спрячет их в git stash.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

# без разделов «Добавлено/Исправлено» пункт считается исправлением, только если об этом сказано прямо
FIX_WORDS = re.compile(r"^(исправ|почин|fix)|\bисправлен|\bfixed\b|\bбаг\b", re.IGNORECASE)
SECTION_ADDED = re.compile(r"^(добавлено|новое|added)\s*:?\s*$", re.IGNORECASE)
SECTION_FIXED = re.compile(r"^(исправлено|fixed|исправления)\s*:?\s*$", re.IGNORECASE)
TRAILER = re.compile(r"^(co-authored-by|signed-off-by)\s*:", re.IGNORECASE)
SEP = "\x1f"
REC = "\x1e"

_color = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def c(text, code):
    return f"\x1b[{code}m{text}\x1b[0m" if _color else str(text)


def bold(t): return c(t, "1")
def dim(t): return c(t, "2")
def green(t): return c(t, "32")
def yellow(t): return c(t, "33")
def red(t): return c(t, "31")
def cyan(t): return c(t, "36")


class Fail(Exception):
    pass


# ---------- git ----------

def repo_root(start: Path | None = None) -> Path:
    here = start or Path(__file__).resolve().parent
    out = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=here, capture_output=True, text=True)
    if out.returncode != 0:
        raise Fail("это не git-репозиторий")
    return Path(out.stdout.strip())


class Git:
    def __init__(self, root: Path):
        self.root = root

    def run(self, *args, check=True) -> str:
        p = subprocess.run(["git", *args], cwd=self.root, capture_output=True, text=True)
        if check and p.returncode != 0:
            raise Fail(f"git {' '.join(args)}: {(p.stderr or p.stdout).strip()}")
        return p.stdout

    def branch(self) -> str | None:
        name = self.run("rev-parse", "--abbrev-ref", "HEAD").strip()
        return None if name == "HEAD" else name

    def head(self) -> str:
        return self.run("rev-parse", "HEAD").strip()

    def dirty(self) -> list[str]:
        """Изменённые и новые (неигнорируемые) файлы. Рабочие данные платформы (backend/data) не в счёт:
        их бережёт keep_data, а в старых версиях они иногда числятся в git."""
        return [line for line in self.run("status", "--porcelain").splitlines()
                if line.strip() and not line[3:].strip('"').startswith(DATA_PREFIX)]

    def resolve(self, ref: str) -> str:
        out = self.run("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False).strip()
        if not out:
            raise Fail(f"нет такой версии: {ref}")
        return out

    def state_path(self) -> Path:
        return Path(self.run("rev-parse", "--absolute-git-dir").strip())

    def state(self) -> dict:
        try:
            return json.loads((self.state_path() / "dragapi-versions.json").read_text("utf-8"))
        except (OSError, ValueError):
            return {}

    def save_state(self, data: dict):
        (self.state_path() / "dragapi-versions.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")


# ---------- разбор коммитов ----------

def parse_body(subject: str, body: str) -> tuple[list[str], list[str]]:
    """«Добавлено:» / «Исправлено:» из текста коммита. Без разделов — пункты «- …» по ключевым словам."""
    added, fixed = [], []
    section = None
    items: list[tuple[str | None, str]] = []
    for raw in body.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped or TRAILER.match(stripped):
            continue
        if SECTION_ADDED.match(stripped):
            section = "added"
            continue
        if SECTION_FIXED.match(stripped):
            section = "fixed"
            continue
        if stripped.startswith(("- ", "* ", "• ")):
            items.append((section, stripped[2:].strip()))
        elif items and line.startswith((" ", "\t")):
            items[-1] = (items[-1][0], items[-1][1] + " " + stripped)    # продолжение пункта
        elif not items:
            items.append((section, stripped))
    for sec, text in items:
        kind = sec or ("fixed" if FIX_WORDS.search(text) else "added")
        (fixed if kind == "fixed" else added).append(text)
    if not added and not fixed:
        (fixed if FIX_WORDS.search(subject) else added).append(subject)
    return added, fixed


def commits(git: Git, limit: int | None = None, ref: str = "HEAD") -> list[dict]:
    args = ["log", ref, f"--format=%H{SEP}%h{SEP}%ad{SEP}%an{SEP}%s{SEP}%b{REC}", "--date=format:%Y-%m-%d %H:%M"]
    if limit:
        args.append(f"-n{limit}")
    out = []
    for chunk in git.run(*args).split(REC):
        chunk = chunk.strip("\n")
        if not chunk:
            continue
        full, short, date, author, subject, body = (chunk.split(SEP) + [""] * 6)[:6]
        added, fixed = parse_body(subject, body)
        out.append({"hash": full, "short": short, "date": date, "author": author, "subject": subject,
                    "added": added, "fixed": fixed})
    for i, item in enumerate(out, 1):
        item["n"] = i
    return out


def pick(git: Git, ref: str, items: list[dict] | None = None) -> dict:
    items = items or commits(git)
    if re.fullmatch(r"\d{1,4}", ref) and 1 <= int(ref) <= len(items):
        return items[int(ref) - 1]
    full = git.resolve(ref)
    for it in items:
        if it["hash"] == full:
            return it
    # версия не из текущей ветки — покажем как есть
    one = commits(git, 1, full)[0]
    one["n"] = None
    return one


# ---------- вывод ----------

def print_list(git: Git, items: list[dict]):
    head = git.head()
    state = git.state()
    print(bold(f"История версий — ветка {git.branch() or '(просмотр версии)'}"))
    print(dim("  №   дата и время      коммит   описание"))
    for it in items:
        mark = green("●") if it["hash"] == head else " "
        counts = []
        if it["added"]:
            counts.append(green(f"+{len(it['added'])}"))
        if it["fixed"]:
            counts.append(yellow(f"✓{len(it['fixed'])}"))
        print(f"{mark}{it['n']:>3}  {dim(it['date'])}  {cyan(it['short'])}  {it['subject']} {' '.join(counts)}")
    print(dim(f"  ● — текущая версия. +N — добавлено пунктов, ✓N — исправлено. Подробно: show <№>"))
    if state.get("viewing"):
        print(yellow(f"  Сейчас открыт просмотр версии; вернуться в «{state['viewing']['from']}»: back"))


def print_show(git: Git, it: dict):
    print(bold(f"{'#' + str(it['n']) + '  ' if it.get('n') else ''}{it['subject']}"))
    print(dim(f"{it['date']} · {it['author']} · {it['hash']}"))
    if it["added"]:
        print(green("\nДобавлено:"))
        for a in it["added"]:
            print(f"  + {a}")
    if it["fixed"]:
        print(yellow("\nИсправлено:"))
        for f in it["fixed"]:
            print(f"  ✓ {f}")
    stat = git.run("show", "--stat", "--format=", "--stat-width=100", it["hash"]).strip().splitlines()
    if stat:
        print(dim("\nИзменённые файлы:"))
        for line in stat[:25]:
            print(dim("  " + line.strip()))
        if len(stat) > 25:
            print(dim(f"  … и ещё {len(stat) - 25}"))


# ---------- действия ----------

DATA_PREFIX = "backend/data/"


@contextmanager
def keep_data(git: Git, target: str):
    """Рабочие данные платформы переживают переключение версии.

    Первые версии по ошибке хранили в git backend/data/projects/example-shop.json. Переход на такую версию
    перезаписал бы живой файл, а возврат — удалил бы его. Поэтому всё из backend/data/, что числится в git
    в текущей или целевой версии, копируется до переключения и возвращается после.
    """
    tracked = set()
    for ref in ("HEAD", target):
        tracked |= {p for p in git.run("ls-tree", "-r", "-z", "--name-only", ref, "--", DATA_PREFIX).split("\0") if p}
    saved = {p: (git.root / p).read_bytes() for p in sorted(tracked) if (git.root / p).is_file()}
    if saved:  # копия на диске — на случай, если процесс оборвётся посередине
        backup = git.state_path() / "dragapi-data-backup" / time.strftime("%Y%m%d-%H%M%S")
        for p, data in saved.items():
            (backup / p).parent.mkdir(parents=True, exist_ok=True)
            (backup / p).write_bytes(data)
    try:
        yield
    finally:
        for p, data in saved.items():
            f = git.root / p
            if not f.is_file() or f.read_bytes() != data:
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_bytes(data)

def ensure_clean(git: Git, stash: bool, what: str):
    changes = git.dirty()
    if not changes:
        return None
    if not stash:
        listing = "\n".join("    " + x for x in changes[:10]) + ("\n    …" if len(changes) > 10 else "")
        raise Fail(f"есть незакоммиченные изменения — {what} их бы потерял:\n{listing}\n"
                   f"  Закоммитьте их или запустите с --stash (они будут спрятаны в git stash).")
    msg = f"versions.py: перед «{what}» {time.strftime('%Y-%m-%d %H:%M')}"
    git.run("stash", "push", "--include-untracked", "-m", msg)
    print(yellow(f"Изменения спрятаны в git stash («{msg}»). Вернуть: git stash pop"))
    return msg


def after_switch(git: Git, rebuild: bool | None):
    print(dim("\nДанные проектов (backend/data) не в git и не меняются. Старые версии платформы могут не знать "
              "новых типов проектов — их просто не будет видно, пока не вернётесь."))
    frontend = git.root / "frontend"
    if rebuild is None and sys.stdin.isatty() and (frontend / "node_modules").exists():
        rebuild = input("Пересобрать интерфейс сейчас (npm run build)? [y/N] ").strip().lower() in ("y", "д", "yes", "да")
    if rebuild:
        print(dim("$ npm run build"))
        r = subprocess.run(["npm", "run", "build"], cwd=frontend)
        print(green("Интерфейс собран.") if r.returncode == 0 else red("Сборка не удалась — см. вывод выше."))
    else:
        print(dim("Интерфейс: cd frontend && npm run build"))
    print(bold("Перезапустите сервер платформы"), dim("(cd backend && .venv/bin/python -m app …), чтобы он работал на этой версии."))


def do_try(git: Git, it: dict, stash: bool, rebuild=None):
    state = git.state()
    origin = (state.get("viewing") or {}).get("from") or git.branch()
    if origin is None:
        raise Fail("сейчас не выбрана ветка — сначала: back")
    ensure_clean(git, stash, "просмотр версии")
    name = f"version/{it['short']}-{it['date'][:10]}"
    with keep_data(git, it["hash"]):
        if git.branch() == name:
            git.run("reset", "--hard", it["hash"])
        else:
            git.run("checkout", "-B", name, it["hash"])   # -B: пересоздать ветку просмотра, если осталась
    git.save_state({**state, "viewing": {"from": origin, "branch": name, "commit": it["hash"]}})
    print(green(f"Открыта версия {it['short']} ({it['date']}) в ветке {name}."))
    print(f"Ваша ветка «{origin}» не изменилась. Вернуться: {bold('python3 scripts/versions.py back')}")
    after_switch(git, rebuild)


def do_back(git: Git, stash: bool, rebuild=None):
    state = git.state()
    viewing = state.get("viewing")
    if not viewing:
        raise Fail("вы не в режиме просмотра версии")
    ensure_clean(git, stash, "возврат")
    with keep_data(git, viewing["from"]):
        git.run("checkout", "--force", viewing["from"])   # --force: данные из backend/data вернёт keep_data
    if git.run("branch", "--list", viewing["branch"]).strip():
        git.run("branch", "-D", viewing["branch"])
    state.pop("viewing", None)
    git.save_state(state)
    print(green(f"Вы снова в ветке «{viewing['from']}»."))
    after_switch(git, rebuild)


def do_rollback(git: Git, it: dict, stash: bool, rebuild=None, assume_yes=False):
    branch = git.branch()
    if branch is None or (git.state().get("viewing") or {}).get("branch") == branch:
        raise Fail("сейчас открыт просмотр версии — сначала вернитесь в свою ветку (back)")
    if it["hash"] == git.head():
        raise Fail("это уже текущая версия")
    if not assume_yes:
        if not sys.stdin.isatty():
            raise Fail("подтвердите откат флагом --yes")
        answer = input(f"Откатить ветку «{branch}» к версии {it['short']} ({it['date']}): «{it['subject']}»? "
                       f"Текущее состояние сохранится в резервной ветке. [y/N] ").strip().lower()
        if answer not in ("y", "д", "yes", "да"):
            print("Отменено.")
            return
    ensure_clean(git, stash, "откат")
    backup = f"backup/{branch}-{time.strftime('%Y%m%d-%H%M%S')}"
    git.run("branch", backup, "HEAD")
    with keep_data(git, it["hash"]):
        git.run("reset", "--hard", it["hash"])
    state = git.state()
    state.setdefault("backups", []).append({"branch": branch, "backup": backup, "from": git.run("rev-parse", backup).strip(),
                                           "to": it["hash"], "t": time.time()})
    git.save_state(state)
    print(green(f"Ветка «{branch}» откачена к {it['short']} ({it['date']})."))
    print(f"Прежнее состояние — в ветке {cyan(backup)}. Отменить откат: {bold('python3 scripts/versions.py undo')}")
    after_switch(git, rebuild)


def do_undo(git: Git, stash: bool, rebuild=None):
    branch = git.branch()
    state = git.state()
    mine = [b for b in state.get("backups", []) if b["branch"] == branch]
    if not mine:
        raise Fail(f"для ветки «{branch}» нет откатов, которые можно отменить")
    last = mine[-1]
    if not git.run("branch", "--list", last["backup"]).strip():
        raise Fail(f"резервной ветки {last['backup']} больше нет")
    ensure_clean(git, stash, "отмена отката")
    with keep_data(git, last["backup"]):
        git.run("reset", "--hard", last["backup"])
    state["backups"].remove(last)
    git.save_state(state)
    print(green(f"Откат отменён: ветка «{branch}» снова на {git.head()[:7]}."))
    print(dim(f"Резервная ветка {last['backup']} оставлена; удалить: git branch -D {last['backup']}"))
    after_switch(git, rebuild)


def print_backups(git: Git):
    names = [b.strip(" *") for b in git.run("branch", "--list", "backup/*").splitlines() if b.strip()]
    if not names:
        print("Резервных копий нет.")
        return
    print(bold("Резервные копии (создаются перед каждым откатом):"))
    for name in names:
        line = git.run("log", "-1", "--format=%h  %ad  %s", "--date=format:%Y-%m-%d %H:%M", name).strip()
        print(f"  {cyan(name)}  {dim(line)}")
    print(dim("Вернуться к копии: git reset --hard <ветка> (или undo — для последнего отката)"))


# ---------- интерактивное меню ----------

def interactive(git: Git, limit: int):
    while True:
        items = commits(git, limit)
        print()
        print_list(git, items)
        choice = input(bold("\nНомер версии (Enter — выход): ")).strip()
        if not choice or choice.lower() in ("q", "й", "exit", "выход"):
            return
        try:
            it = pick(git, choice, items)
        except Fail as e:
            print(red(str(e)))
            continue
        print()
        print_show(git, it)
        print(bold("\nЧто сделать?"))
        print("  1 — посмотреть эту версию (в отдельной ветке, ваша ветка не меняется)")
        print("  2 — откатить текущую ветку к этой версии (с резервной копией)")
        if git.state().get("viewing"):
            print("  3 — вернуться из просмотра в свою ветку")
        print("  Enter — назад к списку")
        action = input("> ").strip()
        try:
            stash = False
            if action in ("1", "2") and git.dirty():
                print(yellow("Есть незакоммиченные изменения:"))
                for line in git.dirty()[:10]:
                    print("    " + line)
                if input("Спрятать их в git stash и продолжить? [y/N] ").strip().lower() not in ("y", "д", "yes", "да"):
                    continue
                stash = True
            if action == "1":
                do_try(git, it, stash)
            elif action == "2":
                do_rollback(git, it, stash)
            elif action == "3":
                do_back(git, False)
        except Fail as e:
            print(red(f"Не получилось: {e}"))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="versions.py", description="История версий DragAPI и откат к любой из них",
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__.split("\n\n", 1)[1])
    ap.add_argument("command", nargs="?", default=None,
                    choices=["list", "show", "try", "back", "rollback", "undo", "backups", "menu"])
    ap.add_argument("version", nargs="?", help="номер из списка (1 — самая новая) или хеш коммита")
    ap.add_argument("-n", type=int, default=40, help="сколько версий показывать")
    ap.add_argument("--stash", action="store_true", help="спрятать незакоммиченные изменения в git stash")
    ap.add_argument("--yes", action="store_true", help="не спрашивать подтверждение отката")
    ap.add_argument("--rebuild", action="store_true", default=None, help="после перехода пересобрать интерфейс")
    ap.add_argument("--repo", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    try:
        git = Git(repo_root(Path(args.repo) if args.repo else None))
        cmd = args.command or ("menu" if sys.stdin.isatty() else "list")
        if cmd in ("show", "try", "rollback") and not args.version:
            raise Fail(f"укажите версию: {cmd} <номер или хеш>")
        if cmd == "menu":
            interactive(git, args.n)
        elif cmd == "list":
            print_list(git, commits(git, args.n))
        elif cmd == "show":
            print_show(git, pick(git, args.version))
        elif cmd == "try":
            do_try(git, pick(git, args.version), args.stash, args.rebuild)
        elif cmd == "back":
            do_back(git, args.stash, args.rebuild)
        elif cmd == "rollback":
            do_rollback(git, pick(git, args.version), args.stash, args.rebuild, args.yes)
        elif cmd == "undo":
            do_undo(git, args.stash, args.rebuild)
        elif cmd == "backups":
            print_backups(git)
    except Fail as e:
        print(red(f"Ошибка: {e}"), file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print()
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
