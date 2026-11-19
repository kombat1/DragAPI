"""Деплой экспортированного проекта в Docker-контейнер на этой машине.

Каждый проект — образ dragapi/<id> и контейнер dragapi-<id> с томом dragapi-<id>-data для БД.
Сборка и запуск идут в фоне с логом (как установка библиотек). Секреты передаются через
временный env-файл (0600), который удаляется сразу после `docker run`.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from .paths import DATA

DEPLOY_DIR = DATA / "deploy"
BUILD_TIMEOUT = 15 * 60
MAX_LOG = 60_000
PORT_RANGE = range(9100, 9200)


def docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "version", "--format", "{{.Server.Version}}"], capture_output=True,
                              timeout=5).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def names(pid: str) -> dict:
    return {"image": f"dragapi/{pid}:latest", "container": f"dragapi-{pid}", "volume": f"dragapi-{pid}-data"}


def free_port(exclude: set[int]) -> int:
    for port in PORT_RANGE:
        if port in exclude:
            continue
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("нет свободных портов в диапазоне 9100–9199")


@dataclass
class Job:
    state: str = "deploying"
    log: str = ""
    started: float = field(default_factory=time.time)
    task: asyncio.Task | None = None

    def write(self, text: str):
        self.log = (self.log + text)[-MAX_LOG:]


async def _run(job: Job | None, *cmd: str, timeout: float = 120, input: bytes | None = None) -> tuple[int, str]:
    if job:
        job.write("$ " + " ".join(cmd) + "\n")
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdin=asyncio.subprocess.PIPE if input else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out = b""

    async def pump():
        nonlocal out
        if input:
            proc.stdin.write(input)
            await proc.stdin.drain()
            proc.stdin.close()
        async for line in proc.stdout:
            out += line
            if job:
                job.write(line.decode("utf-8", "replace"))

    try:
        await asyncio.wait_for(pump(), timeout)
        return await proc.wait(), out.decode("utf-8", "replace")
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        if job:
            job.write(f"\nпрервано: дольше {timeout:g} с\n")
        return -1, out.decode("utf-8", "replace")


class DeployManager:
    def __init__(self):
        self.jobs: dict[str, Job] = {}

    def settings_path(self, pid: str) -> Path:
        return DEPLOY_DIR / pid / "deploy.json"

    def settings(self, pid: str) -> dict:
        try:
            return json.loads(self.settings_path(pid).read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    async def status(self, pid: str) -> dict:
        n = names(pid)
        job = self.jobs.get(pid)
        settings = self.settings(pid)
        info = {"docker": docker_available(), "deploying": bool(job and job.state == "deploying"),
                "log": job.log if job else settings.get("log", ""),
                "settings": {k: v for k, v in settings.items() if k != "log_token"}, **n}
        if not info["docker"]:
            return {**info, "container": None}
        code, out = await _run(None, "docker", "inspect", "--format",
                               "{{json .State}}", n["container"], timeout=10)
        if code != 0:
            return {**info, "container": None}
        state = json.loads(out.strip() or "{}")
        port = settings.get("port")
        host = "127.0.0.1" if not settings.get("public") else "0.0.0.0"
        return {**info, "container": {"status": state.get("Status"), "running": state.get("Running"),
                                      "started": state.get("StartedAt"), "exit_code": state.get("ExitCode")},
                "url": f"http://{'localhost' if host == '127.0.0.1' else host}:{port}" if port else None}

    def start(self, pid: str, files: dict[str, str], env: dict[str, str], port: int | None, public: bool,
              host_gateway: bool = False) -> Job:
        job = self.jobs.get(pid)
        if job and job.state == "deploying":
            raise RuntimeError("деплой уже идёт")
        job = Job()
        self.jobs[pid] = job
        job.task = asyncio.get_running_loop().create_task(self._deploy(pid, files, env, port, public, job, host_gateway))
        return job

    async def _deploy(self, pid, files, env, port, public, job: Job, host_gateway: bool = False):
        n = names(pid)
        base = DEPLOY_DIR / pid
        src = base / "src"
        env_file = base / "run.env"
        ok = False
        try:
            if port is None:
                port = self.settings(pid).get("port") or free_port(set())
            shutil.rmtree(src, ignore_errors=True)
            for path, content in files.items():
                if path == ".env.example":
                    continue
                (src / path).parent.mkdir(parents=True, exist_ok=True)
                (src / path).write_text(content, "utf-8")

            job.write(f"Сборка образа {n['image']}…\n")
            code, _ = await _run(job, "docker", "build", "-t", n["image"], "--label", f"dragapi.project={pid}",
                                 str(src), timeout=BUILD_TIMEOUT)
            if code != 0:
                raise RuntimeError("сборка образа не удалась — подробности выше")

            job.write("\nЗамена контейнера…\n")
            await _run(job, "docker", "rm", "-f", n["container"], timeout=60)
            base.mkdir(parents=True, exist_ok=True)
            fd = os.open(env_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                for k, v in env.items():
                    f.write(f"{k}={str(v).replace(chr(10), ' ')}\n")
            bind = "0.0.0.0" if public else "127.0.0.1"
            extra = []
            if host_gateway:
                extra = ["--add-host", "host.docker.internal:host-gateway"]
                job.write("База PostgreSQL на этой машине: контейнер подключается к ней через host.docker.internal.\n"
                          "PostgreSQL должен принимать подключения с адреса Docker-сети (listen_addresses и "
                          "pg_hba.conf), а не только с 127.0.0.1.\n")
            code, _ = await _run(job, "docker", "run", "-d", "--name", n["container"], "--restart", "unless-stopped",
                                 *extra,
                                 "--label", f"dragapi.project={pid}", "-p", f"{bind}:{port}:8000",
                                 "-v", f"{n['volume']}:/data", "--env-file", str(env_file),
                                 "--security-opt", "no-new-privileges", "--cap-drop", "ALL",
                                 "--memory", "512m", "--pids-limit", "256", n["image"], timeout=120)
            env_file.unlink(missing_ok=True)
            if code != 0:
                raise RuntimeError("контейнер не запустился")

            job.write(f"\nОжидаю готовности http://127.0.0.1:{port}/openapi.json …\n")
            for _ in range(60):
                await asyncio.sleep(0.5)
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/openapi.json", timeout=2) as r:
                        if r.status == 200:
                            ok = True
                            break
                except OSError:
                    continue
            if not ok:
                _, logs = await _run(None, "docker", "logs", "--tail", "50", n["container"], timeout=10)
                job.write("Сервис не ответил за 30 с. Последние строки лога контейнера:\n" + logs)
                raise RuntimeError("сервис в контейнере не поднялся")
            job.write(f"\nГотово: API доступно на порту {port}.\n")
        except Exception as e:  # noqa: BLE001 — любая ошибка деплоя показывается пользователю
            job.write(f"\nОшибка: {e}\n")
        finally:
            env_file.unlink(missing_ok=True)
            job.state = "ready" if ok else "error"
            base.mkdir(parents=True, exist_ok=True)
            self.settings_path(pid).write_text(json.dumps(
                {"port": port, "public": public, "ok": ok, "finished": time.time(), "log": job.log,
                 "log_token": env.get("DRAGAPI_LOG_TOKEN")},
                ensure_ascii=False), "utf-8")
            self.settings_path(pid).chmod(0o600)   # в нём токен журнала запросов

    async def stop(self, pid: str, remove_data: bool = False):
        n = names(pid)
        await _run(None, "docker", "rm", "-f", n["container"], timeout=60)
        if remove_data:
            await _run(None, "docker", "volume", "rm", "-f", n["volume"], timeout=60)
            await _run(None, "docker", "image", "rm", "-f", n["image"], timeout=60)
            shutil.rmtree(DEPLOY_DIR / pid, ignore_errors=True)

    async def logs(self, pid: str, tail: int = 200) -> str:
        code, out = await _run(None, "docker", "logs", "--tail", str(min(max(tail, 1), 2000)),
                               names(pid)["container"], timeout=15)
        return out if code == 0 else ""


deployments = DeployManager()
