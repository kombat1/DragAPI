"""Песочница для блока «Свой Python-код».

Каждый вызов — новый короткоживущий процесс (~75 мс): между запросами не остаётся
состояния. Бэкенды изоляции:

  firejail — без сети, с приватными /home и /tmp, без capabilities, seccomp, с лимитами
             памяти/CPU/процессов/размера файлов. Используется, если установлен.
  process  — обычный подпроцесс только с rlimit-лимитами и пустым окружением. Сеть
             и файловая система доступны — годится лишь для локальной разработки.

Выбор: переменная окружения DRAGAPI_SANDBOX=firejail|process (по умолчанию — лучший доступный).
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import sys
import tempfile
from pathlib import Path
import time
from dataclasses import dataclass

from .compat import IS_WINDOWS, clean_env, set_limits, site_packages, system_python, venv_python

PYTHON = system_python()
MAX_STDOUT = 20_000
MAX_INPUT = 2_000_000
_SEMAPHORES: dict = {}  # по одному на event loop: не больше 4 песочниц одновременно


def _semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    if loop not in _SEMAPHORES:
        _SEMAPHORES.clear()  # старые циклы (например, из тестов) больше не нужны
        _SEMAPHORES[loop] = asyncio.Semaphore(4)
    return _SEMAPHORES[loop]

# Код процесса-исполнителя. Он получает {code, data, env, nonce} на stdin, вызывает run(data, env)
# и печатает результат одной строкой с префиксом-нонсом (чтобы print() пользователя не подделал ответ).
WORKER = r'''
import io, json, sys, traceback
req = json.loads(sys.stdin.read())
nonce = req.pop("nonce")
if req.pop("site_dir", None):
    sys.path.insert(0, "/opt/site")
if req.get("proxy"):
    exec(req.pop("proxy_setup"), {"PROXY_PATH": req.pop("proxy")})
if req.pop("limit_nproc", False):
    import resource
    resource.setrlimit(resource.RLIMIT_NPROC, (1, 1))
real_stdout = sys.stdout
captured = io.StringIO()
sys.stdout = sys.stderr = captured
out = {"ok": False}
try:
    namespace = {"__name__": "user_code"}
    exec(compile(req["code"], "<code>", "exec"), namespace)
    run = namespace.get("run")
    if not callable(run):
        raise NameError("в коде нет функции run(data, env)")
    result = run(req["data"], req["env"])
    json.dumps(result)  # результат должен сериализоваться в JSON
    out = {"ok": True, "result": result}
except BaseException as e:
    tb = traceback.extract_tb(e.__traceback__)
    user_frames = [f for f in tb if f.filename == "<code>"]
    where = f" (строка {user_frames[-1].lineno})" if user_frames else ""
    if isinstance(e, MemoryError):
        msg = "превышен лимит памяти"
    elif isinstance(e, TypeError) and "JSON serializable" in str(e):
        msg = f"результат не сериализуется в JSON: {e}"
    else:
        msg = f"{type(e).__name__}: {e}"
    out = {"ok": False, "error": msg + where,
           "traceback": "".join(traceback.format_list(user_frames))}
out["stdout"] = captured.getvalue()[:%d]
real_stdout.write(nonce + json.dumps(out, ensure_ascii=False, default=str) + "\n")
real_stdout.flush()
''' % MAX_STDOUT


# Выполняется в песочнице до кода пользователя, если исходящий трафик фильтруется.
# Сети у процесса нет: поднимаем на его собственном loopback перенаправитель на unix-сокет
# фильтрующего прокси платформы, выставляем HTTP(S)_PROXY и учим socket.create_connection
# ходить через CONNECT. Потоки ещё разрешены: лимит процессов ставится сразу после этого.
PROXY_SETUP = r'''
import os, selectors, socket, threading

_listener = socket.socket()
_listener.bind(("127.0.0.1", 0))
_listener.listen(32)
_port = _listener.getsockname()[1]


def _forward():
    sel = selectors.DefaultSelector()
    sel.register(_listener, selectors.EVENT_READ)
    peers = {}
    while True:
        for key, _ in sel.select():
            sock = key.fileobj
            if sock is _listener:
                client, _ = _listener.accept()
                try:
                    if PROXY_PATH.startswith("tcp:"):   # Windows: прокси платформы на локальном порту
                        _h, _p = PROXY_PATH[4:].rsplit(":", 1)
                        upstream = socket.socket()
                        upstream.connect((_h, int(_p)))
                    else:
                        upstream = socket.socket(socket.AF_UNIX)
                        upstream.connect(PROXY_PATH)
                except OSError:
                    client.close()
                    continue
                peers[client], peers[upstream] = upstream, client
                sel.register(client, selectors.EVENT_READ)
                sel.register(upstream, selectors.EVENT_READ)
                continue
            other = peers.get(sock)
            try:
                data = sock.recv(65536)
                if data and other is not None:
                    other.sendall(data)
            except OSError:
                data = b""
            if not data:
                for s in (sock, other):
                    if s is not None and s in peers:
                        sel.unregister(s)
                        peers.pop(s, None)
                        s.close()


threading.Thread(target=_forward, daemon=True).start()
_proxy_url = "http://127.0.0.1:" + str(_port)
for _name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
    os.environ[_name] = _proxy_url
for _name in ("NO_PROXY", "no_proxy"):
    os.environ.pop(_name, None)

_original_create_connection = socket.create_connection


def _create_connection(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None, **kwargs):
    host, port = address[0], address[1]
    if host in ("127.0.0.1", "localhost", "::1"):
        return _original_create_connection(address, timeout, source_address, **kwargs)
    sock = _original_create_connection(("127.0.0.1", _port), timeout)
    target = ("[" + host + "]" if ":" in host else host) + ":" + str(port)
    sock.sendall(("CONNECT " + target + " HTTP/1.1\r\nHost: " + target + "\r\n\r\n").encode())
    response = b""
    while b"\r\n\r\n" not in response:
        chunk = sock.recv(4096)
        if not chunk:
            break
        response += chunk
    head, _, body = response.partition(b"\r\n\r\n")
    if head.split(b" ")[1:2] == [b"200"]:
        return sock
    sock.close()
    raise ConnectionRefusedError(body.decode("utf-8", "replace") or "firewall: соединение отклонено")


socket.create_connection = _create_connection
'''


@dataclass
class SandboxResult:
    ok: bool
    result: object = None
    error: str = ""
    traceback: str = ""
    stdout: str = ""
    ms: float = 0
    backend: str = ""
    network: list = None    # события firewall (исходящие соединения) за этот вызов

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in ("ok", "result", "error", "traceback", "stdout", "ms", "backend", "network")}


@dataclass
class Limits:
    timeout: float = 2.0      # секунды «настенного» времени
    memory_mb: int = 256


BACKENDS = ("auto", "firejail", "docker", "process")
DOCKER_IMAGE = f"python:{sys.version_info.major}.{sys.version_info.minor}-slim"  # та же версия, что у venv проекта
_docker_cache: dict = {"t": 0.0, "value": None}


@dataclass
class SandboxSettings:
    """Настройки песочницы проекта (вкладка «Песочница» в настройках проекта)."""
    backend: str = "auto"         # auto | firejail | docker | process
    network: bool = False         # разрешить коду сеть
    threads: bool = False         # разрешить потоки и подпроцессы (нужно многим библиотекам)
    max_file_mb: int = 1          # максимальный размер файла, который код может записать (во временный каталог)
    max_open_files: int = 64

    @classmethod
    def parse(cls, raw: dict | None) -> tuple["SandboxSettings", list[str]]:
        raw = raw or {}
        errors: list[str] = []
        s = cls()
        backend = raw.get("backend", "auto")
        if backend in BACKENDS:
            s.backend = backend
        else:
            errors.append(f"песочница: неизвестный режим «{backend}»")
        s.network = bool(raw.get("network", False))
        s.threads = bool(raw.get("threads", False))
        for name, lo, hi in (("max_file_mb", 0, 100), ("max_open_files", 16, 1024)):
            try:
                value = int(raw.get(name, getattr(s, name)))
                if not lo <= value <= hi:
                    raise ValueError
                setattr(s, name, value)
            except (TypeError, ValueError):
                errors.append(f"песочница: {name} должно быть целым от {lo} до {hi}")
        return s, errors

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in ("backend", "network", "threads", "max_file_mb", "max_open_files")}


def firejail_supported() -> bool:
    return sys.platform.startswith("linux")


def firejail_available() -> bool:
    return firejail_supported() and shutil.which("firejail") is not None


def docker_info() -> dict:
    """Доступен ли Docker, есть ли образ и gVisor (кэш на 30 с)."""
    import subprocess
    now = time.monotonic()
    if _docker_cache["value"] is not None and now - _docker_cache["t"] < 30:
        return _docker_cache["value"]
    info = {"available": False, "image": False, "gvisor": False}
    if shutil.which("docker"):
        try:
            r = subprocess.run(["docker", "info", "--format", "{{range $k, $v := .Runtimes}}{{$k}} {{end}}"],
                               capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                info["available"] = True
                info["gvisor"] = "runsc" in r.stdout.split()
                info["image"] = subprocess.run(["docker", "image", "inspect", DOCKER_IMAGE], capture_output=True,
                                               timeout=5).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            pass
    _docker_cache.update(t=now, value=info)
    return info


def resolve_backend(settings: SandboxSettings | None = None) -> str:
    """Какая песочница будет использована на самом деле (docker — только если выбран явно)."""
    wanted = (settings.backend if settings else "auto")
    if wanted == "auto":
        wanted = os.environ.get("DRAGAPI_SANDBOX", "").strip().lower() or "auto"
    if wanted == "process":
        return "process"
    if wanted == "docker":
        if docker_info()["available"]:
            return "docker"
    return "firejail" if firejail_available() else "process"


def detect_backend() -> str:
    return resolve_backend(None)


def egress_mode(settings: SandboxSettings, firewall=None) -> str:
    if firewall is not None:
        return firewall.egress.mode
    return "open" if settings.network else "off"


def describe(settings: SandboxSettings | None = None, firewall=None) -> dict:
    settings = settings or SandboxSettings()
    backend = resolve_backend(settings)
    isolated = backend == "firejail"
    mode = egress_mode(settings, firewall)
    parts = []
    dinfo = docker_info() if settings.backend == "docker" else {}
    if backend == "docker":
        parts.append("одноразовый контейнер " + ("gVisor" if dinfo.get("gvisor") else "Docker"))
        parts.append({"off": "без сети", "filtered": "сеть только через firewall проекта",
                      "open": "сеть без ограничений"}[mode])
        parts.append("ФС только для чтения, пользователь nobody")
    elif backend == "firejail":
        parts.append("отдельный процесс в firejail")
        parts.append({"off": "без сети", "filtered": "сеть только через firewall проекта",
                      "open": "сеть без ограничений"}[mode])
        parts.append("файлы платформы не видны")
    elif IS_WINDOWS:
        parts.append("отдельный процесс с лимитом времени — сеть и файлы доступны, памяти не ограничена")
    else:
        parts.append("отдельный процесс только с лимитами — сеть и файлы доступны")
    parts.append("потоки разрешены" if settings.threads else "без потоков и подпроцессов")
    warning = ""
    if settings.backend == "docker" and not dinfo.get("available"):
        warning = "Docker недоступен — используется " + backend
    elif backend == "docker" and not dinfo.get("image"):
        warning = f"образ {DOCKER_IMAGE} не загружен: выполните docker pull {DOCKER_IMAGE}"
    elif backend == "docker":
        warning = "медленнее остальных режимов: ~0.5–1 с на вызов"
    elif settings.backend == "firejail" and not firejail_available():
        warning = "firejail не установлен — используется обычный процесс"
    elif backend == "process" and not firejail_supported():
        warning = ("слабая изоляция: на этой ОС firejail нет — для строгой изоляции выберите Docker" + (
            "; правила firewall без изоляции не гарантируются" if mode == "filtered" else ""))
    elif backend == "process":
        warning = "слабая изоляция: установите firejail" + (
            "; правила firewall без firejail не гарантируются" if mode == "filtered" else "")
    elif mode == "open":
        warning = "код блоков может обращаться в сеть без ограничений"
    isolated = isolated or backend == "docker"
    return {"backend": backend, "requested": settings.backend, "isolated": isolated, "egress": mode,
            "docker": docker_info(),
            "firejail_available": firejail_available(), "details": ", ".join(parts), "warning": warning}


def _hint(error: str, settings: SandboxSettings, backend: str, venv: str | None, mode: str = "off") -> str:
    """Подсказка, какую настройку проекта поменять, для типичных ошибок песочницы."""
    if "ModuleNotFoundError" in error and "No module named" in error:
        return " — добавьте пакет в «Настройки проекта → Библиотеки» и установите" + ("" if venv else " (окружение не установлено)")
    if not settings.threads and ("can't start new thread" in error or "Resource temporarily unavailable" in error):
        return " — разрешите потоки и подпроцессы в «Настройки проекта → Песочница»"
    if ("firewall:" in error or "Tunnel connection failed: 403" in error or "403 Forbidden" in error
            or (mode == "filtered" and "ProxyError" in error)):
        return " — соединение запрещено firewall проекта (Настройки проекта → Firewall)"
    if mode == "off" and backend == "firejail" and (
            "Network is unreachable" in error or "Temporary failure in name resolution" in error
            or "Name or service not known" in error or "Connection refused" in error):
        return " — сеть отключена: включите её в «Настройки проекта → Firewall»"
    return ""


def _docker_command(name: str, limits: Limits, settings: SandboxSettings, venv: str | None,
                    mode: str, proxy_dir: str | None) -> list[str]:
    mem = int(limits.memory_mb)
    cpu = max(1, int(limits.timeout + 0.999))
    pids = 128 if settings.threads else (2 if mode == "filtered" else 1)  # поток-перенаправитель firewall
    cmd = ["docker", "run", "--rm", "-i", "--name", name, "--pull", "never",
           "--network", "bridge" if mode == "open" else "none",
           "--memory", f"{mem}m", "--memory-swap", f"{mem}m", "--cpus", "1", "--pids-limit", str(pids),
           "--read-only", "--tmpfs", f"/tmp:rw,noexec,nosuid,size={max(settings.max_file_mb, 1)}m",
           "--user", "65534:65534", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
           "--ulimit", f"nofile={settings.max_open_files}:{settings.max_open_files}",
           "--ulimit", f"cpu={cpu}:{cpu}", "--label", "dragapi.sandbox=1",
           "-e", "LC_ALL=C.UTF-8", "-e", "PYTHONIOENCODING=utf-8", "-e", "OPENBLAS_NUM_THREADS=1",
           "-e", "OMP_NUM_THREADS=1", "-w", "/tmp"]
    if docker_info()["gvisor"]:
        cmd += ["--runtime", "runsc"]
    if venv:
        site = site_packages(venv)
        if site:
            cmd += ["-v", f"{site}:/opt/site:ro"]
    if proxy_dir:
        cmd += ["-v", f"{proxy_dir}:{proxy_dir}"]
    return cmd + [DOCKER_IMAGE, "python", "-I", "-S", "-c", WORKER]


def _command(backend: str, limits: Limits, settings: SandboxSettings, venv: str | None,
             mode: str = "off", proxy_dir: str | None = None) -> tuple[list[str], dict]:
    mem = int(limits.memory_mb) * 1024 * 1024
    cpu = max(1, int(limits.timeout + 0.999))
    fsize = settings.max_file_mb * 1024 * 1024
    python = str(venv_python(venv)) if venv else PYTHON
    # -I: без PYTHONPATH и пользовательского site; -S (без site) — только без venv, иначе пакеты не найдутся
    flags = ["-I"] if venv else ["-I", "-S"]
    if backend == "firejail":
        worker = ["env", "-i", "LC_ALL=C.UTF-8", "PYTHONIOENCODING=utf-8", "OPENBLAS_NUM_THREADS=1",
                  "OMP_NUM_THREADS=1", "MKL_NUM_THREADS=1", python, *flags, "-c", WORKER]
        # без venv домашний каталог полностью приватный; с venv виден только он, и только на чтение
        fs = [f"--whitelist={venv}", f"--read-only={venv}"] if venv else []
        if proxy_dir:  # сокет фильтрующего прокси — единственный путь наружу
            fs += [f"--whitelist={proxy_dir}"]
        if not fs:
            fs = ["--private"]
        # при фильтрации лимит процессов ставит сам worker — после запуска своего потока-перенаправителя
        nproc = [] if settings.threads or mode == "filtered" else ["--rlimit-nproc=1"]
        return [
            "firejail", "--quiet", "--noprofile", *([] if mode == "open" else ["--net=none"]), *fs,
            "--private-tmp", "--private-dev", "--caps.drop=all", "--nonewprivs", "--seccomp", "--nogroups",
            "--nosound", "--no3d", f"--rlimit-as={mem}", f"--rlimit-cpu={cpu}",
            *nproc,
            f"--rlimit-fsize={fsize}", f"--rlimit-nofile={settings.max_open_files}", *worker,
        ], {}

    # backend=process: почти пустое окружение и лимиты ресурсов там, где они есть (Linux, macOS)
    worker = [python, *flags, "-c", WORKER]
    if IS_WINDOWS:   # лимитов памяти и процессов нет — остаётся лимит времени
        return worker, {"env": clean_env()}

    def preexec():
        limits_ = {"RLIMIT_AS": (mem, mem), "RLIMIT_CPU": (cpu, cpu), "RLIMIT_FSIZE": (fsize, fsize),
                   "RLIMIT_NOFILE": (settings.max_open_files, settings.max_open_files)}
        if not settings.threads and mode != "filtered":
            limits_["RLIMIT_NPROC"] = (1, 1)  # лимит на пользователя: новые процессы не создать
        set_limits(limits_)
        os.setsid()

    return worker, {"preexec_fn": preexec, "env": clean_env()}


async def run_code(code: str, data: dict, env: dict, limits: Limits | None = None,
                   settings: SandboxSettings | None = None, venv: str | None = None,
                   firewall=None) -> SandboxResult:
    """venv — окружение проекта с библиотеками (или None); firewall — FirewallSettings проекта."""
    settings = settings or SandboxSettings()
    mode = egress_mode(settings, firewall)
    if mode != "filtered":
        return await _run(code, data, env, limits, settings, venv, mode, None)
    from .firewall import EgressProxy
    async with EgressProxy(firewall.egress) as proxy:
        result = await _run(code, data, env, limits, settings, venv, mode, proxy)
    result.network = proxy.events
    return result


async def _run(code, data, env, limits, settings, venv, mode, proxy) -> SandboxResult:
    limits = limits or Limits()
    backend = resolve_backend(settings)
    nonce = secrets.token_hex(16)
    request = {"code": code, "data": data, "env": env, "nonce": nonce}
    if proxy is not None:
        # в Docker лимит потоков задаёт --pids-limit; setrlimit там считал бы процессы nobody на всём хосте
        request.update(proxy=proxy.path, proxy_setup=PROXY_SETUP,
                       limit_nproc=not settings.threads and backend != "docker")
    if backend == "docker" and venv:
        request["site_dir"] = True
    payload = json.dumps(request, ensure_ascii=False, default=str)
    if len(payload) > MAX_INPUT:
        return SandboxResult(False, error="слишком большие входные данные для блока кода", backend=backend)

    container = f"dragapi-sbx-{nonce[:12]}"
    if backend == "docker":
        if not docker_info()["image"]:
            return SandboxResult(False, backend=backend,
                                 error=f"образ {DOCKER_IMAGE} не загружен — выполните: docker pull {DOCKER_IMAGE}")
        if proxy is not None:  # пользователь nobody в контейнере должен достучаться до сокета прокси
            os.chmod(proxy.dir, 0o711)
            os.chmod(proxy.path, 0o666)
        cmd, extra = _docker_command(container, limits, settings, venv, mode, proxy.dir if proxy else None), {}
    else:
        cmd, extra = _command(backend, limits, settings, venv, mode, proxy.dir if proxy else None)
    # запуск контейнера дольше процесса — даём ему время на старт сверх лимита кода
    startup = 5.0 if backend == "docker" else 0.5
    loop = asyncio.get_running_loop()
    started = loop.time()
    async with _semaphore():
        with tempfile.TemporaryDirectory(prefix="dragapi-sbx-") as cwd:
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=cwd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, **extra,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(payload.encode()), limits.timeout + startup)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                if backend == "docker":  # убить сам контейнер: обрыв `docker run` его не останавливает
                    killer = await asyncio.create_subprocess_exec(
                        "docker", "rm", "-f", container, stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL)
                    await killer.wait()
                hint = (" — сетевые запросы бывают медленными: увеличьте лимит времени блока"
                        if mode != "off" else "")
                return SandboxResult(False, error=f"превышено время выполнения ({limits.timeout:g} с){hint}",
                                     ms=round((loop.time() - started) * 1000, 1), backend=backend)
    ms = round((loop.time() - started) * 1000, 1)

    for line in stdout.decode("utf-8", "replace").splitlines():
        if line.startswith(nonce):
            out = json.loads(line[len(nonce):])
            error = out.get("error", "")
            if error:
                error += _hint(error, settings, backend, venv, mode)
            return SandboxResult(out["ok"], out.get("result"), error, out.get("traceback", ""),
                                 out.get("stdout", ""), ms, backend)
    # процесс умер, не успев ответить: лимит CPU/памяти или сбой песочницы
    reason = "процесс песочницы завершился без ответа"
    if proc.returncode in (-9, -24, 137, 152) or "OOMKilled" in stderr.decode("utf-8", "replace"):
        reason = "превышен лимит CPU или памяти"
    tail = stderr.decode("utf-8", "replace").strip()[-300:]
    return SandboxResult(False, error=reason + (f": {tail}" if tail else ""), ms=ms, backend=backend)
