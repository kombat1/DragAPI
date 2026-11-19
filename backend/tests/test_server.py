"""Запуск на сервере: HTTPS (свой и самоподписанный сертификат), прокси, защита от открытой платформы."""

import io
import os
import ssl
import zipfile

import pytest
from fastapi.testclient import TestClient

from app import agents, auth, config, storage, tls
from app import main as platform_main


# ---------- что можно и что нельзя запускать ----------


def test_plan_local_by_default():
    p = tls.plan({}, auth_off=True)
    assert not p.errors and not p.warnings
    assert (p.host, p.port, p.scheme, p.mode) == ("127.0.0.1", 8765, "http", None)
    assert p.url == "http://localhost:8765"


@pytest.mark.parametrize("cfg", [{"host": "0.0.0.0"}, {"host": "192.168.1.5"}, {"behind_proxy": True}])
def test_plan_refuses_open_platform_without_login(cfg):
    p = tls.plan(cfg, auth_off=True)
    assert p.errors and "без входа" in p.errors[0]
    assert not tls.plan(cfg, auth_off=False).errors


def test_plan_warns_about_plain_http_in_network():
    p = tls.plan({"host": "0.0.0.0"}, auth_off=False)
    assert not p.errors and any("без HTTPS" in w for w in p.warnings)
    assert not tls.plan({"host": "0.0.0.0", "tls_self_signed": True}, auth_off=False).warnings


def test_plan_tls_modes(tmp_path):
    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    cert.write_text("x")
    key.write_text("x")
    p = tls.plan({"tls_cert": str(cert), "tls_key": str(key), "port": 443, "host": "0.0.0.0"}, auth_off=False)
    assert (p.mode, p.scheme, p.cert, p.key, p.errors) == ("cert", "https", str(cert), str(key), [])
    assert "нужны оба" in tls.plan({"tls_cert": str(cert)}, auth_off=False).errors[0]
    assert any("не найден" in e for e in tls.plan({"tls_cert": "/nope", "tls_key": str(key)}, auth_off=False).errors)
    assert any("что-то одно" in e for e in
               tls.plan({"tls_cert": str(cert), "tls_key": str(key), "tls_self_signed": True}, auth_off=False).errors)
    p = tls.plan({"behind_proxy": True}, auth_off=False)
    assert (p.mode, p.scheme, p.errors, p.warnings) == ("proxy", "http", [], [])
    assert tls.plan({"behind_proxy": True, "host": "0.0.0.0"}, auth_off=False).warnings


def test_config_accepts_server_keys(tmp_path, monkeypatch):
    f = tmp_path / "dragapi.toml"
    f.write_text('tls_self_signed = true\ntls_names = ["dragapi.lan"]\nbehind_proxy = false\ntrusted_proxies = "10.0.0.2"\n')
    monkeypatch.setenv("DRAGAPI_CONFIG", str(f))
    assert config.load()["tls_names"] == ["dragapi.lan"]
    f.write_text("tls_names = [1, 2]\n")
    with pytest.raises(config.ConfigError, match="список строк"):
        config.load()


# ---------- самоподписанный сертификат ----------


def _sans(path):
    from cryptography import x509
    cert = x509.load_pem_x509_certificate(path.read_bytes())
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    return {str(v) for v in san.get_values_for_type(x509.DNSName)} | {str(v) for v in san.get_values_for_type(x509.IPAddress)}


def test_self_signed_issue_reuse_and_extend(tmp_path):
    names = ["localhost", "127.0.0.1", "dragapi.lan"]
    cert, key, fresh = tls.ensure_self_signed(names, tmp_path)
    assert fresh and _sans(cert) >= {"localhost", "127.0.0.1", "dragapi.lan"}
    if os.name != "nt":
        assert oct((tmp_path / "server-key.pem").stat().st_mode & 0o777) == "0o600"
    # сертификат сервера проверяется по CA платформы
    ctx = ssl.create_default_context(cafile=str(tls.ca_path(tmp_path)))
    ctx.load_cert_chain(cert, key)
    ca_before = tls.ca_path(tmp_path).read_bytes()
    # те же имена — ничего не перевыпускается
    assert tls.ensure_self_signed(["dragapi.lan", "localhost"], tmp_path)[2] is False
    # новое имя — новый сертификат сервера, CA прежний (агентам и браузерам не нужно ничего менять)
    cert2, _, fresh2 = tls.ensure_self_signed(names + ["10.1.2.3"], tmp_path)
    assert fresh2 and "10.1.2.3" in _sans(cert2)
    assert tls.ca_path(tmp_path).read_bytes() == ca_before
    assert len(tls.fingerprint(tls.ca_path(tmp_path)).split(":")) == 32


def test_local_names_include_loopback_and_extra():
    names = tls.local_names(["dragapi.lan"], "10.0.0.7")
    assert names[:3] == ["localhost", "127.0.0.1", "::1"]
    assert "dragapi.lan" in names and "10.0.0.7" in names
    assert "0.0.0.0" not in tls.local_names(None, "0.0.0.0")
    assert len(names) == len(set(names))


# ---------- сессия и агенты ----------


@pytest.fixture
def platform(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path / "projects")
    monkeypatch.setattr(storage, "DB_DIR", tmp_path / "db")
    monkeypatch.delenv("DRAGAPI_AUTH", raising=False)
    yield tmp_path


@pytest.mark.parametrize("base, secure", [("https://dragapi.lan", True), ("http://testserver", False)])
def test_session_cookie_secure_only_over_https(platform, base, secure):
    with TestClient(platform_main.app, base_url=base, headers={"X-DragAPI": "1"}) as c:
        r = c.post("/api/auth/setup", json={"username": "admin", "password": "secret-pass-123"})
        assert r.status_code == 200, r.text
        assert ("Secure" in r.headers["set-cookie"]) is secure


def test_agent_bundle_carries_platform_ca(tmp_path, monkeypatch):
    names = lambda data: zipfile.ZipFile(io.BytesIO(data)).namelist()  # noqa: E731
    monkeypatch.delenv("DRAGAPI_TLS_MODE", raising=False)
    assert "platform-ca.pem" not in names(agents.agent_bundle())
    tls.ensure_self_signed(["localhost"], tmp_path)
    monkeypatch.setattr(tls, "TLS_DIR", tmp_path)
    monkeypatch.setenv("DRAGAPI_TLS_MODE", "self-signed")
    z = zipfile.ZipFile(io.BytesIO(agents.agent_bundle()))
    assert z.read("platform-ca.pem") == tls.ca_path(tmp_path).read_bytes()
