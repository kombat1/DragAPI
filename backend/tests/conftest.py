import pytest
from cryptography.fernet import Fernet

from app import vault


@pytest.fixture(autouse=True)
def _test_secret_key(monkeypatch):
    """Каждый тест — со своим ключом шифрования, настоящий data/secret.key не трогаем."""
    monkeypatch.setenv("DRAGAPI_SECRET_KEY", Fernet.generate_key().decode())
    vault.reset_cache()
    yield
    vault.reset_cache()
