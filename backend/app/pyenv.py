"""Виртуальные окружения проектов: библиотеки для блоков «Python-код».

Каталог проекта: data/envs/<id>/
  venv/              готовое окружение (его видит песочница, только на чтение)
  venv.new/          окружение, которое сейчас собирается
  requirements.txt   что ставили в последний раз
  state.json         итог последней установки
  cache/, home/      кэш pip и $HOME для pip

Установка всегда собирает окружение с нуля в venv.new и атомарно подменяет venv: пока идёт
установка, запросы продолжают работать на старом окружении, а удалённые из списка пакеты
действительно исчезают. pip запускается в firejail: сеть есть, но виден только каталог проекта.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from .paths import DATA

ENVS_DIR = DATA / "envs"
PYTHON = "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else "python3"
INSTALL_TIMEOUT = 15 * 60
MAX_LOG = 60_000
MAX_PACKAGES = 50

_NAME = r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?"
_VERSION = r"[A-Za-z0-9.*+!_-]+"
_SPEC = rf"(?:===|==|>=|<=|~=|!=|<|>)\s*{_VERSION}"
REQ_RE = re.compile(rf"^{_NAME}(?:\[{_NAME}(?:\s*,\s*{_NAME})*\])?\s*(?:{_SPEC}(?:\s*,\s*{_SPEC})*)?$")


def validate_packages(raw) -> tuple[list[str], list[str]]:
    """Только «имя[extras] спецификатор_версии». Опции pip, URL, пути и -e запрещены."""
    packages, errors = [], []
    for line in raw or []:
        line = str(line).strip()
        if not line or line.startswith("#"):
            continue
        if not REQ_RE.match(line):
            errors.append(f"библиотеки: «{line}» — укажите имя пакета и, при желании, версию "
                          f"(numpy, requests==2.32.3, pandas>=2.0). Опции pip, URL и пути запрещены")
            continue
        packages.append(line)
    if len(packages) > MAX_PACKAGES:
        errors.append(f"библиотеки: не больше {MAX_PACKAGES} пакетов")
    return packages, errors


def requirements_hash(packages: list[str], only_binary: bool) -> str:
    key = json.dumps({"p": sorted(p.lower().replace(" ", "") for p in packages), "b": only_binary})
    return hashlib.sha256(key.encode()).hexdigest()[:16]


@dataclass
class Job:
    state: str = "installing"
    log: str = ""
    started: float = field(default_factory=time.time)
    task: asyncio.Task | None = None

    def write(self, text: str):
        self.log = (self.log + text)[-MAX_LOG:]


class EnvManager:
    def __init__(self, root: Path | None = None):
        self.root = root or ENVS_DIR
        self.jobs: dict[str, Job] = {}

    # --- пути и состояние ---

    def dir(self, pid: str) -> Path:
        return self.root / pid

    def venv(self, pid: str) -> str | None:
        """Готовое окружение проекта или None (тогда код работает на системном Python без пакетов)."""
        path = self.dir(pid) / "venv"
        return str(path) if (path / "bin" / "python").exists() else None

    def _state(self, pid: str) -> dict:
        try:
            return json.loads((self.dir(pid) / "state.json").read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def status(self, pid: str, packages: list[str] | None = None, only_binary: bool = True) -> dict:
        state = self._state(pid)
        job = self.jobs.get(pid)
        installed_hash = state.get("hash") if state.get("ok") and self.venv(pid) else None
        wanted_hash = requirements_hash(packages or [], only_binary) if packages is not None else None
        if job and job.state == "installing":
            status = "installing"
        elif not packages and not self.venv(pid):
            status = "none"
        elif not self.venv(pid):
            status = "missing"
        elif installed_hash and installed_hash == wanted_hash:
            status = "ready"
        elif state and not state.get("ok"):
            status = "error"
        else:
            status = "outdated"
        return {
            "status": status,
            "installed": state.get("installed", []) if self.venv(pid) else [],
            "packages": state.get("packages", []),
            "finished": state.get("finished"),
            "python": state.get("python"),
            "size_mb": state.get("size_mb"),
            "log": job.log if job else state.get("log", ""),
            "started": job.started if job and job.state == "installing" else None,
        }

    def is_current(self, pid: str, packages: list[str], only_binary: bool) -> bool:
        if not packages:
            return True
        state = self._state(pid)
        return bool(state.get("ok") and self.venv(pid) and state.get("hash") == requirements_hash(packages, only_binary))

    # --- установка ---

    def start_install(self, pid: str, packages: list[str], only_binary: bool, use_firejail: bool) -> Job:
        job = self.jobs.get(pid)
        if job and job.state == "installing":
            raise RuntimeError("установка уже идёт")
        job = Job()
        self.jobs[pid] = job
        job.task = asyncio.get_running_loop().create_task(
            self._install(pid, packages, only_binary, use_firejail, job))
        return job

    async def _run(self, job: Job, cmd: list[str], timeout: float) -> int:
        job.write("$ " + " ".join(c if len(c) < 120 else c[:117] + "..." for c in cmd) + "\n")
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, stdin=asyncio.subprocess.DEVNULL)

        async def pump():
            async for line in proc.stdout:
                job.write(line.decode("utf-8", "replace"))

        try:
            await asyncio.wait_for(pump(), timeout)
            return await proc.wait()
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            job.write(f"\nпрервано: дольше {timeout // 60:g} мин\n")
            return -1

    async def _install(self, pid: str, packages: list[str], only_binary: bool, use_firejail: bool, job: Job):
        base = self.dir(pid)
        new = base / "venv.new"
        ok = False
        installed: list[str] = []
        python_version = ""
        try:
            base.mkdir(parents=True, exist_ok=True)
            for d in ("cache", "home"):
                (base / d).mkdir(exist_ok=True)
            shutil.rmtree(new, ignore_errors=True)
            (base / "requirements.txt").write_text("\n".join(packages) + "\n", "utf-8")

            job.write("Создаю окружение…\n")
            if await self._run(job, [PYTHON, "-m", "venv", str(new)], 120) != 0:
                raise RuntimeError("не удалось создать виртуальное окружение")

            pip = [str(new / "bin" / "python"), "-m", "pip", "install", "--disable-pip-version-check",
                   "--no-input", "--progress-bar", "off"]
            if only_binary:
                pip.append("--only-binary=:all:")
            if packages:
                pip += ["-r", str(base / "requirements.txt")]
                env = ["env", "-i", "PATH=/usr/bin:/bin", "LC_ALL=C.UTF-8", f"HOME={base / 'home'}",
                       f"PIP_CACHE_DIR={base / 'cache'}"]
                sandbox = (["firejail", "--quiet", "--noprofile", f"--whitelist={base}", "--private-tmp",
                            "--private-dev", "--caps.drop=all", "--nonewprivs", "--seccomp", "--nogroups"]
                           if use_firejail else [])
                job.write(f"\nУстанавливаю {len(packages)} пакет(ов)"
                          f"{' в firejail' if use_firejail else ''}…\n")
                if await self._run(job, [*sandbox, *env, *pip], INSTALL_TIMEOUT) != 0:
                    raise RuntimeError("pip завершился с ошибкой — подробности выше")

            freeze = await asyncio.create_subprocess_exec(
                str(new / "bin" / "python"), "-m", "pip", "freeze", "--disable-pip-version-check",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            out, _ = await freeze.communicate()
            installed = [line for line in out.decode().splitlines() if line.strip()]
            ver = await asyncio.create_subprocess_exec(
                str(new / "bin" / "python"), "-c", "import sys; print(sys.version.split()[0])",
                stdout=asyncio.subprocess.PIPE)
            python_version = (await ver.communicate())[0].decode().strip()
            # pip в готовом окружении не нужен: код видит его только на чтение, а каждая установка
            # собирает окружение заново. Без pip окружение в несколько раз меньше.
            await self._run(job, [str(new / "bin" / "python"), "-m", "pip", "uninstall", "-y", "-q",
                                  "--disable-pip-version-check", "pip"], 120)

            # атомарная подмена: старое окружение работает до последнего момента
            old = base / "venv.old"
            shutil.rmtree(old, ignore_errors=True)
            if (base / "venv").exists():
                (base / "venv").rename(old)
            new.rename(base / "venv")
            shutil.rmtree(old, ignore_errors=True)
            ok = True
            job.write(f"\nГотово: установлено {len(installed)} пакет(ов).\n")
        except Exception as e:  # noqa: BLE001 — любая ошибка установки показывается пользователю
            job.write(f"\nОшибка: {e}\n")
            shutil.rmtree(new, ignore_errors=True)
        finally:
            job.state = "ready" if ok else "error"
            # символические ссылки (bin/python -> системный интерпретатор) не считаем
            size = sum(f.lstat().st_size for f in (base / "venv").rglob("*") if f.is_file() and not f.is_symlink()) \
                if (base / "venv").exists() else 0
            state = {
                "ok": ok, "hash": requirements_hash(packages, only_binary) if ok else None,
                "packages": packages, "only_binary": only_binary, "installed": installed,
                "python": python_version, "finished": time.time(), "size_mb": round(size / 1e6, 1),
                "log": job.log,
            }
            if not ok and self._state(pid).get("ok"):
                # старое окружение осталось рабочим — помним, что в нём стоит
                prev = self._state(pid)
                state.update(installed=prev.get("installed", []), python=prev.get("python"),
                             size_mb=prev.get("size_mb"), packages=prev.get("packages", []))
            if base.exists():
                (base / "state.json").write_text(json.dumps(state, ensure_ascii=False), "utf-8")

    def remove(self, pid: str):
        job = self.jobs.pop(pid, None)
        if job and job.task and not job.task.done():
            job.task.cancel()
        shutil.rmtree(self.dir(pid), ignore_errors=True)


envs = EnvManager()
