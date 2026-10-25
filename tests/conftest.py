import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import common  # noqa: E402
import pipeline  # noqa: E402
import tg_publish  # noqa: E402


TEST_SCHEDULE = {
    "timezone": "Asia/Krasnoyarsk", "publish_mode": "dry-run", "min_dry_runs_before_live": 2,
    "window_end": "now", "max_window_days": 8, "default_window_days": 7, "slot_grace_hours": 14,
    "retry_after_hours": [6, 12], "max_rejected_ratio": 0.4, "history_weeks": 8,
    "issues": {
        "weekly": {"enabled": True, "title": "AI Weekly", "day": "sunday", "time": "10:00",
                   "target_items": [12, 20], "min_items": 8, "cover": True},
        "midweek": {"enabled": False, "title": "Главное за середину недели", "day": "wednesday", "time": "18:00",
                    "target_items": [6, 10], "min_items": 4, "cover": False},
    },
}


@pytest.fixture(autouse=True)
def fixed_schedule(monkeypatch):
    """Тесты не зависят от боевого config/schedule.yaml; отдельные тесты подменяют SCHEDULE сами."""
    import copy

    sched = copy.deepcopy(TEST_SCHEDULE)
    real = common.load_yaml

    def fake(name):
        return sched if name == "schedule.yaml" else real(name)

    for mod in (common, pipeline):
        monkeypatch.setattr(mod, "load_yaml", fake)
    return sched


@pytest.fixture(autouse=True)
def isolate_from_real_telegram(monkeypatch):
    """Тесты никогда не читают настоящий .env и не ходят в api.telegram.org."""
    for mod in (common, pipeline, tg_publish):
        monkeypatch.setattr(mod, "load_dotenv", lambda *a, **k: None)
    for var in common.SECRET_ENV:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TELEGRAM_API_BASE", "http://127.0.0.1:9")  # закрытый порт; e2e-тест подменяет сам
