"""Avtomatik sotuv: Analytics qabul qilgan buyurtma reaksiyasiz Ombor'ga (owner 2026-10-21)."""
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.analytics_reader import ApiAnalyticsReader
from app.auto_sync import WATERMARK_KEY, auto_sync_once
from app.state import StateStore
from tests.test_corrections import ombor_recorder
from tests.test_sync_logic import make_config

NOW = datetime(2026, 10, 21, 5, 0, tzinfo=timezone.utc)
CHAT = -100123


def _order(oid, msg, items, *, accepted=True, created=NOW - timedelta(hours=2), deleted=False, bonus=()):
    return {"order_id": oid, "chat_id": CHAT, "message_id": msg, "accepted": accepted, "is_deleted": deleted,
            "review_status": "APPROVED" if accepted else "PENDING", "created_at": created.isoformat(),
            "changed_at": created.isoformat(), "items": [{"product": p, "units_total": q} for p, q in items],
            "bonus_items": [{"product": p, "quantity": q} for p, q in bonus]}


class Changes:
    def __init__(self, orders):
        self.orders, self.calls = orders, []

    async def fetch_changes(self, since, limit):
        self.calls.append(since)
        return [o for o in self.orders if datetime.fromisoformat(o["changed_at"]) > since][:limit]


@pytest.mark.asyncio
async def test_accepted_orders_pushed_without_reaction(state_db_path):
    a = Changes([
        _order(1501, 11, [("qayla", 4)]),
        _order(1502, 12, [("palov", 20), ("dimlama", 20)], bonus=[("non", 2)]),
        _order(1503, 13, [("palov", 5)], accepted=False),                 # ko'rib chiqishda - yo'q
        dict(_order(1504, 14, [("palov", 9)], created=NOW - timedelta(days=10)),
             changed_at=(NOW - timedelta(hours=1)).isoformat()),             # eski buyurtma tahrirlandi - yo'q
    ])
    calls, state = [], StateStore(state_db_path)
    s = await auto_sync_once(make_config(), a, ombor_recorder(calls), state, now=NOW)
    pushed = [(b["order_reference"], b["items"]) for kind, b in calls if kind != "alert"]
    assert pushed == [("1501", [{"code": "qayla", "quantity": "4"}]),
                      ("1502", [{"code": "dimlama", "quantity": "20"}, {"code": "non", "quantity": "2"},
                                {"code": "palov", "quantity": "20"}])]
    assert (s.synced, s.skipped) == (2, 2)
    assert state.get_meta(WATERMARK_KEY) is not None

    # ikkinchi aylanish - takror yuborilmaydi
    calls.clear()
    s2 = await auto_sync_once(make_config(), a, ombor_recorder(calls), state, now=NOW)
    assert [c for c in calls if c[0] != "alert"] == [] and s2.synced == 0
    state.close()


@pytest.mark.asyncio
async def test_later_accepted_order_is_picked_up(state_db_path):
    pending = _order(1503, 13, [("palov", 5)], accepted=False)
    a = Changes([pending])
    calls, state = [], StateStore(state_db_path)
    await auto_sync_once(make_config(), a, ombor_recorder(calls), state, now=NOW)
    assert [c for c in calls if c[0] != "alert"] == []
    approved = dict(pending, accepted=True, review_status="APPROVED", changed_at=(NOW - timedelta(minutes=5)).isoformat())
    a.orders = [approved]
    await auto_sync_once(make_config(), a, ombor_recorder(calls), state, now=NOW)
    assert [b["order_reference"] for k, b in calls if k != "alert"] == ["1503"]
    state.close()


@pytest.mark.asyncio
async def test_reader_without_changes_is_skipped(state_db_path, analytics):
    s = await auto_sync_once(make_config(), analytics, ombor_recorder([]), StateStore(state_db_path), now=NOW)
    assert s.seen == 0


def test_postgres_reader_has_changes():
    from app.analytics_reader import PostgresAnalyticsReader
    assert hasattr(PostgresAnalyticsReader("postgresql://x"), "fetch_changes")


@pytest.mark.asyncio
async def test_api_reader_fetch_changes():
    seen = {}

    def handler(request):
        seen["path"], seen["params"] = request.url.path, dict(request.url.params)
        seen["auth"] = request.headers["authorization"]
        return httpx.Response(200, json=[_order(1, 2, [("palov", 1)])])
    r = ApiAnalyticsReader("analytics.test", "tok", transport=httpx.MockTransport(handler))
    out = await r.fetch_changes(NOW, 50)
    assert seen == {"path": "/internal/orders/changes", "params": {"since": NOW.isoformat(), "limit": "50"},
                    "auth": "Bearer tok"} and out[0]["order_id"] == 1


def test_next_run_end_of_day_and_catch_up():
    from zoneinfo import ZoneInfo
    from app.auto_sync import next_run, parse_run_time
    seoul = ZoneInfo("Asia/Seoul")
    at = parse_run_time("23:50")
    morning = datetime(2026, 10, 21, 10, 0, tzinfo=seoul)
    assert next_run(morning, at, "2026-10-20") == datetime(2026, 10, 21, 23, 50, tzinfo=seoul)
    late = datetime(2026, 10, 21, 23, 55, tzinfo=seoul)
    assert next_run(late, at, "2026-10-20") == late                    # o'tkazib yuborilgan - darhol
    assert next_run(late, at, "2026-10-21") == datetime(2026, 10, 22, 23, 50, tzinfo=seoul)
    assert parse_run_time("off") is None and parse_run_time("bad") == at


@pytest.mark.asyncio
async def test_daily_loop_runs_once_per_day(state_db_path, monkeypatch):
    from app import auto_sync
    monkeypatch.setenv("AUTO_SYNC_TIME", "23:50")
    a = Changes([_order(1601, 21, [("qayla", 4)])])
    calls, state, slept = [], StateStore(state_db_path), []

    async def fake_sleep(sec):
        slept.append(sec)
    clock = lambda: datetime(2026, 10, 21, 14, 50, tzinfo=timezone.utc)   # KST 23:50
    await auto_sync.run_daily_loop(make_config(), a, ombor_recorder(calls), state, sleep=fake_sleep,
                                   clock=clock, max_runs=1)
    assert [b["order_reference"] for k, b in calls if k != "alert"] == ["1601"]
    assert state.get_meta(auto_sync.LAST_RUN_KEY) == "2026-10-21" and slept == [0.0]


@pytest.mark.asyncio
async def test_ordered_at_sent_to_ombor(state_db_path):
    """Ombor sanoqdan oldingi buyurtmani qayta ayirmasligi uchun buyurtma vaqti yuboriladi (2026-10-24)."""
    created = NOW - timedelta(hours=5)
    a = Changes([_order(1701, 31, [("qayla", 4)], created=created)])
    calls, state = [], StateStore(state_db_path)
    await auto_sync_once(make_config(), a, ombor_recorder(calls), state, now=NOW)
    body = next(b for k, b in calls if k != "alert")
    assert body["ordered_at"] == created.isoformat() and body["system"] == "analytics"
