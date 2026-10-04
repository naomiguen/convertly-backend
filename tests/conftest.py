import pytest

from app.core.config import settings
from app.core.redis_backend import manager


@pytest.fixture(autouse=True)
def _isolate_redis(monkeypatch):
    """Tes tidak boleh menyentuh Redis sungguhan walau .env mengisi REDIS_URL.
    Tes Redis (test_redis_backend.py) memasang server palsu sendiri."""
    monkeypatch.setattr(settings, "REDIS_URL", "")
    manager.set_client_factory(None)
    yield
    manager.set_client_factory(None)
