import pytest
from cryptography.fernet import Fernet

from app import vault


@pytest.fixture(autouse=True)
def _test_secret_key(monkeypatch, tmp_path):
    """Каждый тест — со своим ключом шифрования и хранилищем пользователей; настоящие data/* не трогаем.

    Вход по умолчанию выключен (DRAGAPI_AUTH=off) — тесты входа включают его сами.
    """
    from app import auth
    monkeypatch.setenv("DRAGAPI_SECRET_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DRAGAPI_AUTH", "off")
    monkeypatch.setattr(auth, "store", auth.AuthStore(tmp_path / "auth"))
    vault.reset_cache()
    yield
    vault.reset_cache()
