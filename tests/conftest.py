import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import common  # noqa: E402
import pipeline  # noqa: E402
import tg_publish  # noqa: E402


@pytest.fixture(autouse=True)
def isolate_from_real_telegram(monkeypatch):
    """Тесты никогда не читают настоящий .env и не ходят в api.telegram.org."""
    for mod in (common, pipeline, tg_publish):
        monkeypatch.setattr(mod, "load_dotenv", lambda *a, **k: None)
    for var in common.SECRET_ENV:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TELEGRAM_API_BASE", "http://127.0.0.1:9")  # закрытый порт; e2e-тест подменяет сам
