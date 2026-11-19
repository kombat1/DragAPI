"""HTTPS и работа на сервере.

Три режима (выбираются в dragapi.toml или флагами python -m app):

* tls_cert + tls_key — свой сертификат (например, от Let's Encrypt);
* tls_self_signed  — платформа сама выпускает себе сертификат: локальный центр сертификации (CA) и
  сертификат сервера, подписанный им. Файлы — в data/tls/. CA можно добавить в доверенные на своих
  компьютерах, тогда браузер не будет ругаться; удалённые агенты получают его в zip автоматически;
* behind_proxy     — HTTPS делает обратный прокси (Caddy, nginx), платформа слушает 127.0.0.1 и верит
  заголовкам X-Forwarded-* только от trusted_proxies.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import socket
from dataclasses import dataclass, field
from pathlib import Path

from .paths import DATA

TLS_DIR = DATA / "tls"
LOOPBACK = {"127.0.0.1", "localhost", "::1", "[::1]"}
CA_DAYS = 3650
SERVER_DAYS = 825          # больше браузеры не принимают
RENEW_BEFORE_DAYS = 30


class TLSError(Exception):
    pass


@dataclass
class ServerPlan:
    host: str
    port: int
    scheme: str                     # http | https — как пользователь открывает платформу
    cert: str | None = None
    key: str | None = None
    mode: str | None = None         # None | cert | self-signed | proxy
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def url(self) -> str:
        host = "localhost" if self.host in ("127.0.0.1", "0.0.0.0", "::", "") else self.host
        return f"{self.scheme}://{host}:{self.port}"


def is_loopback(host: str) -> bool:
    if host in LOOPBACK:
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def plan(cfg: dict, *, auth_off: bool) -> ServerPlan:
    """Как запускать сервер и что в этой конфигурации опасно. Ошибки — отказ запускаться."""
    host = cfg.get("host") or "127.0.0.1"
    port = int(cfg.get("port") or 8765)
    cert, key = cfg.get("tls_cert"), cfg.get("tls_key")
    self_signed = bool(cfg.get("tls_self_signed"))
    proxy = bool(cfg.get("behind_proxy"))
    p = ServerPlan(host=host, port=port, scheme="http")

    if bool(cert) != bool(key):
        p.errors.append("для HTTPS нужны оба файла: tls_cert и tls_key")
    if (cert or key) and self_signed:
        p.errors.append("выберите что-то одно: свой сертификат (tls_cert/tls_key) или tls_self_signed")
    if proxy and (cert or self_signed):
        p.warnings.append("за обратным прокси HTTPS обычно делает прокси — встроенный HTTPS тоже включён")
    for f in (cert, key):
        if f and not Path(f).is_file():
            p.errors.append(f"файл не найден: {f}")
    if cert and key:
        p.mode, p.cert, p.key, p.scheme = "cert", cert, key, "https"
    elif self_signed:
        p.mode, p.scheme = "self-signed", "https"
    elif proxy:
        p.mode = "proxy"

    exposed = not is_loopback(host) or proxy
    if auth_off and exposed:
        where = "за обратным прокси" if proxy else f"на адресе {host}"
        p.errors.append(
            f"без входа платформу можно запускать только на 127.0.0.1, а не {where}: любой, кто достучится "
            "до сервера, получит полный доступ (и к мыши с клавиатурой этой машины). Уберите --no-auth / "
            "auth = false — при первом входе платформа предложит создать администратора")
    if not is_loopback(host) and p.scheme == "http" and not proxy:
        p.warnings.append(
            f"платформа открыта в сеть ({host}) без HTTPS: пароли, токены ботов и агентов идут открытым "
            "текстом. Включите tls_self_signed, свой сертификат или поставьте прокси (см. deploy/)")
    if proxy and not is_loopback(host):
        p.warnings.append(f"behind_proxy: обычно платформа слушает 127.0.0.1, а наружу смотрит только прокси (сейчас {host})")
    return p


# ---------- самоподписанный сертификат ----------


def local_names(extra: list[str] | None = None, host: str | None = None) -> list[str]:
    """Имена и адреса, по которым к этой машине могут обратиться: попадут в сертификат."""
    names = ["localhost", "127.0.0.1", "::1"]
    try:
        hn = socket.gethostname()
        names += [hn, f"{hn}.local"]
        fq = socket.getfqdn()
        if fq and fq != hn:
            names.append(fq)
        for info in socket.getaddrinfo(hn, None):
            names.append(info[4][0])
    except OSError:
        pass
    try:  # основной адрес в сети (без отправки пакетов: UDP connect только выбирает маршрут)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            names.append(s.getsockname()[0])
    except OSError:
        pass
    if host and host not in ("0.0.0.0", "::", ""):
        names.append(host)
    names += extra or []
    out = []
    for n in names:
        n = str(n).strip().strip("[]")
        if n and "%" not in n and n not in out:
            out.append(n)
    return out


def _san(names: list[str]):
    from cryptography import x509
    items = []
    for n in names:
        try:
            items.append(x509.IPAddress(ipaddress.ip_address(n)))
        except ValueError:
            items.append(x509.DNSName(n))
    return x509.SubjectAlternativeName(items)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _key():
    from cryptography.hazmat.primitives.asymmetric import ec
    return ec.generate_private_key(ec.SECP256R1())


def _write_key(path: Path, key):
    from cryptography.hazmat.primitives import serialization
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()))
    path.chmod(0o600)


def _load_cert(path: Path):
    from cryptography import x509
    try:
        return x509.load_pem_x509_certificate(path.read_bytes())
    except (OSError, ValueError):
        return None


def ca_path(directory: Path | None = None) -> Path:
    return (directory or TLS_DIR) / "ca.pem"


def ensure_self_signed(names: list[str], directory: Path | None = None) -> tuple[Path, Path, bool]:
    """Локальный CA + сертификат сервера для names. Выпускает заново, если имён не хватает или срок
    подходит к концу. -> (cert, key, выпущен_заново)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    d = directory or TLS_DIR
    d.mkdir(parents=True, exist_ok=True)
    ca_file, ca_key_file = d / "ca.pem", d / "ca-key.pem"
    cert_file, key_file = d / "server.pem", d / "server-key.pem"
    soon = _now() + dt.timedelta(days=RENEW_BEFORE_DAYS)

    ca = _load_cert(ca_file)
    ca_key = None
    if ca is not None and ca.not_valid_after_utc > soon and ca_key_file.exists():
        ca_key = serialization.load_pem_private_key(ca_key_file.read_bytes(), None)
    else:
        ca_key = _key()
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"DragAPI local CA ({socket.gethostname()})"),
                          x509.NameAttribute(NameOID.ORGANIZATION_NAME, "DragAPI")])
        ca = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(ca_key.public_key())
              .serial_number(x509.random_serial_number())
              .not_valid_before(_now() - dt.timedelta(minutes=5)).not_valid_after(_now() + dt.timedelta(days=CA_DAYS))
              .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
              .add_extension(x509.KeyUsage(digital_signature=False, content_commitment=False, key_encipherment=False,
                                           data_encipherment=False, key_agreement=False, key_cert_sign=True,
                                           crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
              .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
              .sign(ca_key, hashes.SHA256()))
        _write_key(ca_key_file, ca_key)
        ca_file.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
        cert_file.unlink(missing_ok=True)   # старый сертификат подписан прежним CA

    cert = _load_cert(cert_file)
    if cert is not None and key_file.exists() and cert.not_valid_after_utc > soon and cert.issuer == ca.subject:
        have = {str(v) for v in cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
                .get_values_for_type(x509.DNSName)}
        have |= {str(v) for v in cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
                 .get_values_for_type(x509.IPAddress)}
        want = {str(ipaddress.ip_address(n)) if _is_ip(n) else n for n in names}
        if want <= have:
            return cert_file, key_file, False

    key = _key()
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0] if names else "localhost")]))
            .issuer_name(ca.subject).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(_now() - dt.timedelta(minutes=5)).not_valid_after(_now() + dt.timedelta(days=SERVER_DAYS))
            .add_extension(_san(names), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    _write_key(key_file, key)
    # цепочка: сертификат сервера + CA — клиентам проще проверить
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM) + ca.public_bytes(serialization.Encoding.PEM))
    return cert_file, key_file, True


def _is_ip(n: str) -> bool:
    try:
        ipaddress.ip_address(n)
        return True
    except ValueError:
        return False


def fingerprint(path: Path) -> str:
    from cryptography.hazmat.primitives import hashes
    cert = _load_cert(path)
    if cert is None:
        return ""
    return ":".join(f"{b:02X}" for b in cert.fingerprint(hashes.SHA256()))


def active_ca() -> Path | None:
    """CA самоподписанного режима, если платформа сейчас в нём работает (для zip агента)."""
    import os
    if os.environ.get("DRAGAPI_TLS_MODE") != "self-signed":
        return None
    p = ca_path()
    return p if p.exists() else None
