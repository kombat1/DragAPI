"""Firewall проекта.

Исходящий трафик (код блоков «Python-код» → сеть). Режимы:
  off       — сети нет совсем (firejail --net=none);
  filtered  — сети у песочницы по-прежнему нет, единственный выход — фильтрующий HTTP-прокси
              платформы через unix-сокет. Прокси сам разрешает DNS, проверяет правила и соединяется
              с уже проверенным IP (защита от DNS rebinding). Обойти фильтр нельзя: другого маршрута нет;
  open      — общая сеть хоста без ограничений.

Внутри песочницы worker поднимает на своём loopback перенаправитель на unix-сокет прокси,
выставляет HTTP(S)_PROXY и подменяет socket.create_connection, поэтому работают urllib,
requests, httpx и «сырые» TCP-соединения через socket.create_connection.

Входящий трафик (клиенты → API проекта) фильтрует IngressFirewall из runtime.py — он же
попадает в экспортированный проект.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import shutil
import socket
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

FW_DIR = Path(__file__).resolve().parent.parent / "data" / "fw"
EGRESS_MODES = ("off", "filtered", "open")
HOST_RE = re.compile(r"^(\*|(\*\.)?([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)$")
MAX_RULES = 200
MAX_CONNECTIONS = 16      # одновременных соединений из одного вызова кода
CONNECT_TIMEOUT = 10


def is_private(ip: ipaddress._BaseAddress) -> bool:
    """Локальная сеть, loopback, link-local (в т.ч. метаданные облака 169.254.169.254) и т.п."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved
            or ip.is_unspecified or ip in ipaddress.ip_network("100.64.0.0/10"))


# ---------------- правила ----------------

def _parse_ports(raw) -> list[tuple[int, int]] | None:
    """'' -> любые; '443'; '80,443'; '8000-8100'."""
    text = str(raw or "").strip()
    if not text or text == "*":
        return []
    ranges = []
    for part in text.split(","):
        part = part.strip()
        m = re.fullmatch(r"(\d{1,5})(?:\s*-\s*(\d{1,5}))?", part)
        if not m:
            return None
        lo, hi = int(m.group(1)), int(m.group(2) or m.group(1))
        if not (1 <= lo <= hi <= 65535):
            return None
        ranges.append((lo, hi))
    return ranges


@dataclass
class Rule:
    action: str                       # allow | deny
    target: str
    network: ipaddress._BaseNetwork | None = None
    host: str | None = None           # 'api.github.com', '*.github.com' или '*'
    ports: list[tuple[int, int]] = field(default_factory=list)
    comment: str = ""

    def port_ok(self, port: int) -> bool:
        return not self.ports or any(lo <= port <= hi for lo, hi in self.ports)

    def host_ok(self, host: str) -> bool:
        if self.host is None:
            return False
        if self.host == "*":
            return True
        if self.host.startswith("*."):
            return host.endswith(self.host[1:])
        return host == self.host

    def ip_ok(self, ip) -> bool:
        return self.network is not None and ip.version == self.network.version and ip in self.network

    @property
    def overrides_private(self) -> bool:
        """Явное разрешение конкретного адреса/имени снимает запрет локальных адресов; '*' и 0.0.0.0/0 — нет."""
        if self.host is not None:
            return self.host != "*"
        return self.network is not None and self.network.prefixlen > 0


def parse_rule(raw: dict, *, allow_hosts: bool, allow_ports: bool) -> tuple[Rule | None, str | None]:
    action = raw.get("action", "allow")
    if action not in ("allow", "deny"):
        return None, f"неизвестное действие «{action}»"
    target = str(raw.get("target") or "").strip().lower().rstrip(".")
    if not target:
        return None, "пустой адрес"
    rule = Rule(action, target, comment=str(raw.get("comment") or ""))
    try:
        rule.network = ipaddress.ip_network(target, strict=False)
    except ValueError:
        if not allow_hosts:
            return None, f"«{target}» — укажите IP-адрес или подсеть (192.168.1.10, 10.0.0.0/8)"
        if not HOST_RE.match(target):
            return None, f"«{target}» — не IP, не подсеть и не имя хоста (api.example.com, *.example.com)"
        rule.host = target
    if allow_ports:
        ports = _parse_ports(raw.get("ports"))
        if ports is None:
            return None, f"«{raw.get('ports')}» — порты: 443, 80,443 или 8000-8100"
        rule.ports = ports
    return rule, None


@dataclass
class Verdict:
    allowed: bool
    reason: str
    ip: str | None = None
    rule: int | None = None  # номер правила (с 1)


@dataclass
class EgressPolicy:
    mode: str = "off"
    default: str = "deny"               # deny | allow — если ни одно правило не подошло
    block_private: bool = True
    rules: list[Rule] = field(default_factory=list)
    resolver: object = None             # для тестов: async (host, port) -> [ip_str]

    async def resolve(self, host: str, port: int) -> list[str]:
        if self.resolver:
            return await self.resolver(host, port)
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
        seen, out = set(), []
        for info in infos:
            addr = info[4][0]
            if addr not in seen:
                seen.add(addr)
                out.append(addr)
        return out

    async def decide(self, host: str, port: int) -> Verdict:
        host = host.strip().lower().rstrip(".").strip("[]")
        if not 1 <= port <= 65535:
            return Verdict(False, f"некорректный порт {port}")
        try:
            ips = [ipaddress.ip_address(host)]
            by_name = False
        except ValueError:
            if not HOST_RE.match(host) or host == "*":
                return Verdict(False, f"некорректное имя хоста «{host}»")
            by_name = True
            try:
                ips = [ipaddress.ip_address(a) for a in await self.resolve(host, port)]
            except (OSError, ValueError) as e:
                return Verdict(False, f"не удалось разрешить имя {host}: {e}")
            if not ips:
                return Verdict(False, f"имя {host} не разрешается")
        first = None
        for ip in ips:
            verdict = self._decide_ip(host if by_name else None, ip, port)
            if verdict.allowed:
                return verdict
            first = first or verdict
        return first

    def _decide_ip(self, host: str | None, ip, port: int) -> Verdict:
        private = self.block_private and is_private(ip)
        for i, rule in enumerate(self.rules, 1):
            if not rule.port_ok(port):
                continue
            if not ((host and rule.host_ok(host)) or rule.ip_ok(ip)):
                continue
            if rule.action == "deny":
                return Verdict(False, f"запрещено правилом {i} ({rule.target})", str(ip), i)
            if private and not rule.overrides_private:
                return Verdict(False, f"{ip} — локальный адрес; правило {i} ({rule.target}) слишком общее, "
                                      f"разрешите этот адрес явно", str(ip), i)
            return Verdict(True, f"разрешено правилом {i} ({rule.target})", str(ip), i)
        if private:
            return Verdict(False, f"{ip} — локальная сеть или метаданные облака (запрещено настройкой)", str(ip))
        if self.default == "allow":
            return Verdict(True, "разрешено по умолчанию", str(ip))
        return Verdict(False, "нет разрешающего правила", str(ip))


@dataclass
class IngressPolicy:
    enabled: bool = False
    default: str = "allow"
    trust_proxy: bool = False
    rules: list[Rule] = field(default_factory=list)

    def runtime_rules(self) -> list[dict]:
        """Простое представление для runtime.IngressFirewall (и для экспорта)."""
        return [{"action": r.action, "target": r.target} for r in self.rules]


@dataclass
class FirewallSettings:
    egress: EgressPolicy = field(default_factory=EgressPolicy)
    ingress: IngressPolicy = field(default_factory=IngressPolicy)

    @classmethod
    def parse(cls, raw: dict | None, legacy_network: bool = False) -> tuple["FirewallSettings", list[str]]:
        raw = raw or {}
        errors: list[str] = []
        fw = cls()

        eg = raw.get("egress") or {}
        mode = eg.get("mode") or ("open" if legacy_network else "off")
        if mode not in EGRESS_MODES:
            errors.append(f"firewall: неизвестный режим исходящего трафика «{mode}»")
            mode = "off"
        fw.egress.mode = mode
        fw.egress.default = "allow" if eg.get("default") == "allow" else "deny"
        fw.egress.block_private = bool(eg.get("block_private", True))
        for i, r in enumerate((eg.get("rules") or [])[:MAX_RULES], 1):
            rule, error = parse_rule(r, allow_hosts=True, allow_ports=True)
            if error:
                errors.append(f"firewall, исходящие, правило {i}: {error}")
            else:
                fw.egress.rules.append(rule)

        ing = raw.get("ingress") or {}
        fw.ingress.enabled = bool(ing.get("enabled"))
        fw.ingress.default = "deny" if ing.get("default") == "deny" else "allow"
        fw.ingress.trust_proxy = bool(ing.get("trust_proxy"))
        for i, r in enumerate((ing.get("rules") or [])[:MAX_RULES], 1):
            rule, error = parse_rule(r, allow_hosts=False, allow_ports=False)
            if error:
                errors.append(f"firewall, входящие, правило {i}: {error}")
            else:
                fw.ingress.rules.append(rule)
        if fw.ingress.enabled and fw.ingress.default == "deny" and not any(
                r.action == "allow" for r in fw.ingress.rules):
            errors.append("firewall, входящие: всё запрещено и нет ни одного разрешающего правила — API будет недоступно")
        return fw, errors


# ---------------- фильтрующий прокси ----------------

class EgressProxy:
    """HTTP-прокси на unix-сокете для одного вызова кода. Поддерживает CONNECT и absolute-form HTTP."""

    def __init__(self, policy: EgressPolicy):
        self.policy = policy
        self.events: list[dict] = []
        self.dir: str | None = None
        self.path: str | None = None
        self._server = None
        self._tasks: set[asyncio.Task] = set()
        self._writers: set[asyncio.StreamWriter] = set()

    async def __aenter__(self):
        FW_DIR.mkdir(parents=True, exist_ok=True)
        self.dir = tempfile.mkdtemp(prefix="p", dir=FW_DIR)
        self.path = str(Path(self.dir) / "proxy.sock")
        self._server = await asyncio.start_unix_server(self._handle, self.path, limit=64 * 1024)
        return self

    async def __aexit__(self, *exc):
        self._server.close()
        for w in list(self._writers):
            w.close()
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        shutil.rmtree(self.dir, ignore_errors=True)

    def _log(self, method, host, port, verdict: Verdict):
        if len(self.events) < 200:
            self.events.append({"time": time.time(), "direction": "out", "method": method, "host": host,
                                "port": port, "ip": verdict.ip, "allowed": verdict.allowed, "reason": verdict.reason})

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        task = asyncio.current_task()
        self._tasks.add(task)
        self._writers.add(writer)
        upstream_writer = None
        try:
            if len(self._tasks) > MAX_CONNECTIONS:
                await self._reply(writer, 429, "firewall: слишком много одновременных соединений")
                return
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), CONNECT_TIMEOUT)
            lines = head.decode("latin-1").split("\r\n")
            method, target, version = (lines[0].split(" ") + ["", "", ""])[:3]
            if method == "CONNECT":
                host, _, port_s = target.rpartition(":")
                if not port_s.isdigit():
                    await self._reply(writer, 400, "firewall: CONNECT host:port")
                    return
                port, path_line = int(port_s), None
            else:
                url = urlsplit(target)
                if url.scheme != "http" or not url.hostname:
                    await self._reply(writer, 400, "firewall: поддерживаются CONNECT и http://")
                    return
                host, port = url.hostname, url.port or 80
                path = (url.path or "/") + (f"?{url.query}" if url.query else "")
                path_line = f"{method} {path} {version or 'HTTP/1.1'}"

            verdict = await self.policy.decide(host, port)
            self._log(method, host.strip("[]").lower(), port, verdict)
            if not verdict.allowed:
                await self._reply(writer, 403, f"firewall: {host}:{port} — {verdict.reason}")
                return
            try:
                up_reader, upstream_writer = await asyncio.wait_for(
                    asyncio.open_connection(verdict.ip, port), CONNECT_TIMEOUT)
            except (OSError, asyncio.TimeoutError) as e:
                await self._reply(writer, 502, f"firewall: не удалось подключиться к {host}:{port}: {e}")
                return
            self._writers.add(upstream_writer)

            if path_line is None:
                writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                await writer.drain()
            else:
                headers = [h for h in lines[1:] if h and not h.lower().startswith(("proxy-", "connection:"))]
                if not any(h.lower().startswith("host:") for h in headers):
                    headers.insert(0, f"Host: {host}:{port}")
                upstream_writer.write(("\r\n".join([path_line, *headers, "Connection: close"]) + "\r\n\r\n")
                                      .encode("latin-1"))
                await upstream_writer.drain()

            await asyncio.gather(self._pipe(reader, upstream_writer), self._pipe(up_reader, writer))
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError, ConnectionError):
            pass
        finally:
            for w in (writer, upstream_writer):
                if w is not None:
                    w.close()
                    self._writers.discard(w)
            self._tasks.discard(task)

    @staticmethod
    async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            try:
                writer.write_eof()
            except (OSError, RuntimeError):
                pass

    @staticmethod
    async def _reply(writer: asyncio.StreamWriter, status: int, text: str):
        reason = {400: "Bad Request", 403: "Forbidden", 429: "Too Many Requests", 502: "Bad Gateway"}[status]
        body = text.encode("utf-8")
        writer.write(f"HTTP/1.1 {status} {reason}\r\nContent-Type: text/plain; charset=utf-8\r\n"
                     f"X-DragAPI-Firewall: deny\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n"
                     .encode() + body)
        try:
            await writer.drain()
        except ConnectionError:
            pass
