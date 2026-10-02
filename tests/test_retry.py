"""Avtomatik qayta urinish: FAILED / NEEDS_REVIEW buyurtmalar o'zi yoziladi."""
import sqlite3
from types import SimpleNamespace

import httpx
import pytest

from app.retry import retry_once, run_retry_loop
from app.state import StateStore
from tests.test_sync_logic import default_handler_factory, make_config, make_ombor


def _config(**kw):
    from dataclasses import replace
    return replace(make_config(), **kw)


class Flaky:
    """Soxta Ombor: ombor_down=True bo'lsa 503, aks holda mapping bo'yicha."""
    def __init__(self, mapping=None):
        self.pushed, self.down = [], True
        self.alerts = []
        self.inner = default_handler_factory(self.pushed, mapping)

    def __call__(self, request):
        if request.url.path == "/system-alerts":
            import json
            self.alerts.append(json.loads(request.content))
            return httpx.Response(201, json={})
        if self.down:
            return httpx.Response(503, json={"detail": "Ombor ishlamayapti"})
        return self.inner(request)


@pytest.mark.asyncio
async def test_failed_order_is_synced_automatically_when_ombor_recovers(analytics, state_db_path):
    from app.sync_logic import process_reaction
    analytics.add_order(chat_id=-100123, message_id=80, order_id=980, items=[("palov", 2)])
    fake = Flaky()
    ombor = make_ombor(fake)
    state = StateStore(state_db_path)
    config = _config()

    first = await process_reaction(-100123, 80, config, analytics, ombor, state)
    assert first.outcome == "failed"

    s1 = await retry_once(config, analytics, ombor, state)  # Ombor hali ishlamayapti
    assert (s1.checked, s1.synced, s1.still_failing) == (1, 0, 1)
    assert fake.pushed == []

    fake.down = False
    s2 = await retry_once(config, analytics, ombor, state)  # tiklandi - o'zi yozildi
    assert (s2.checked, s2.synced) == (1, 1)
    assert len(fake.pushed) == 1 and fake.pushed[0]["source_id"] == "analytics-order:980"
    assert [a["level"] for a in fake.alerts][-1] == "recovered"

    s3 = await retry_once(config, analytics, ombor, state)  # endi ro'yxatda yo'q
    assert s3.checked == 0
    state.close()


@pytest.mark.asyncio
async def test_needs_review_resolves_after_owner_maps_code_in_ombor(analytics, state_db_path):
    from app.sync_logic import process_reaction
    analytics.add_order(chat_id=-100123, message_id=81, order_id=981, items=[("somsa", 3)])
    mapping = {"palov": ["PALOV"]}
    fake = Flaky(mapping)
    fake.down = False
    ombor = make_ombor(fake)
    state = StateStore(state_db_path)
    config = _config()
    notified = []

    async def notify(chat_id, message_id, result):
        notified.append(result.reason)

    first = await process_reaction(-100123, 81, config, analytics, ombor, state)
    assert first.outcome == "needs_review"

    await retry_once(config, analytics, ombor, state, notify)
    assert notified == []  # sabab o'zgarmagan - qayta xabar yo'q

    mapping["somsa"] = ["SOMSA_GOSHT"]  # owner Ombor botida bog'ladi
    s = await retry_once(config, analytics, ombor, state, notify)
    assert s.synced == 1 and fake.pushed[0]["items"][0]["finished_product_external_code"] == "SOMSA_GOSHT"
    state.close()


@pytest.mark.asyncio
async def test_old_and_other_statuses_are_not_retried(analytics, state_db_path):
    state = StateStore(state_db_path)
    state.mark_failed("-100123:1", reason="x")
    state.mark_not_an_order("-100123:2")
    state.mark_synced("-100123:3")
    state.mark_needs_review("-100123:4", reason="y")
    state.mark_failed("garbage-key", reason="z")
    # eski yozuv - 30 kun oldin birinchi ko'rilgan
    state.mark_failed("-100123:5", reason="old")
    state._conn.execute("UPDATE processed_reactions SET first_seen_at='2020-01-01T00:00:00+00:00' "
                        "WHERE sync_key='-100123:5'")
    state._conn.commit()
    keys = [i.sync_key for i in state.list_retryable(max_age_days=7, limit=50)]
    assert sorted(keys) == ["-100123:1", "-100123:4"]
    state.close()


def test_first_seen_is_kept_on_later_updates(state_db_path):
    state = StateStore(state_db_path)
    state.mark_failed("-1:1", reason="a")
    first = state._conn.execute("SELECT first_seen_at FROM processed_reactions").fetchone()[0]
    state.mark_failed("-1:1", reason="b")
    assert state._conn.execute("SELECT first_seen_at FROM processed_reactions").fetchone()[0] == first
    state.close()


def test_legacy_database_gets_column(tmp_path):
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE processed_reactions (sync_key TEXT PRIMARY KEY, status TEXT NOT NULL, "
                 "reason TEXT, processed_at TEXT NOT NULL)")
    conn.execute("INSERT INTO processed_reactions VALUES ('-1:9', 'FAILED', 'x', '2099-01-01T00:00:00+00:00')")
    conn.commit(); conn.close()
    state = StateStore(path)  # ustun qo'shiladi, eski yozuv processed_at bo'yicha hisoblanadi
    assert [i.sync_key for i in state.list_retryable(max_age_days=7, limit=10)] == ["-1:9"]
    state.close()


@pytest.mark.asyncio
async def test_loop_disabled_and_survives_errors(monkeypatch):
    import app.retry as retry
    calls = []

    async def boom(*a, **k):
        calls.append(1)
        raise RuntimeError("x")
    monkeypatch.setattr(retry, "retry_once", boom)

    async def no_sleep(_):
        return None
    await run_retry_loop(SimpleNamespace(retry_interval_minutes=0, retry_max_age_days=7),
                         None, None, None, sleep=no_sleep, max_cycles=3)
    assert calls == []
    await run_retry_loop(SimpleNamespace(retry_interval_minutes=10, retry_max_age_days=7),
                         None, None, None, sleep=no_sleep, max_cycles=3)
    assert len(calls) == 3  # xato siklni to'xtatmadi


def test_int_env(monkeypatch):
    from app.config import _int_env
    monkeypatch.delenv("X_TEST", raising=False)
    assert _int_env("X_TEST", 10) == 10
    monkeypatch.setenv("X_TEST", "0")
    assert _int_env("X_TEST", 10) == 0
    for bad in ("abc", "-1"):
        monkeypatch.setenv("X_TEST", bad)
        with pytest.raises(RuntimeError):
            _int_env("X_TEST", 10)
