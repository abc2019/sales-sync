"""sales-sync -> Analytics ichki API (ERP kontrakti, Analytics #57)."""
from types import SimpleNamespace

import httpx
import pytest

from app.analytics_reader import ApiAnalyticsReader, PostgresAnalyticsReader, build_reader

ORDER = {"order_id": 990, "chat_id": -100123, "message_id": 77, "category": "OPTOM", "review_status": "APPROVED",
         "accepted": True, "is_deleted": False, "created_at": "2026-10-04T03:00:00+00:00",
         "changed_at": "2026-10-04T03:00:00+00:00", "items": [{"product": "palov", "units_total": 24}],
         "bonus_items": [], "items_hash": "abc", "sale_total_krw": None}


def _reader(handler):
    return ApiAnalyticsReader("analytics.test", "tok", transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_reads_order_via_api():
    seen = {}

    def handler(request):
        seen.update(url=str(request.url), auth=request.headers["authorization"])
        return httpx.Response(200, json=ORDER)
    o = await _reader(handler).fetch_order_by_message(-100123, 77)
    assert seen["url"] == "https://analytics.test/internal/orders/by-message?chat_id=-100123&message_id=77"
    assert seen["auth"] == "Bearer tok"
    assert (o.order_id, o.accepted, o.review_status) == (990, True, "APPROVED")
    assert o.items[0].product_code == "palov" and o.items[0].units_total == 24


@pytest.mark.asyncio
async def test_not_found_deleted_and_pending():
    assert await _reader(lambda r: httpx.Response(404, json={})).fetch_order_by_message(1, 1) is None
    assert await _reader(lambda r: httpx.Response(200, json={**ORDER, "is_deleted": True})).fetch_order_by_message(1, 1) is None
    o = await _reader(lambda r: httpx.Response(200, json={**ORDER, "accepted": False, "review_status": "PENDING"})).fetch_order_by_message(1, 1)
    assert o.accepted is False and o.review_status == "PENDING"


@pytest.mark.asyncio
async def test_server_error_raises_for_retry():
    with pytest.raises(httpx.HTTPStatusError):
        await _reader(lambda r: httpx.Response(503, json={})).fetch_order_by_message(1, 1)


def test_build_reader_prefers_api():
    cfg = SimpleNamespace(analytics_api_base_url="https://a", analytics_api_token="t", analytics_database_url="postgres://x")
    assert isinstance(build_reader(cfg), ApiAnalyticsReader)
    cfg2 = SimpleNamespace(analytics_api_base_url=None, analytics_api_token=None, analytics_database_url="postgres://x")
    assert isinstance(build_reader(cfg2), PostgresAnalyticsReader)
    with pytest.raises(RuntimeError):
        build_reader(SimpleNamespace(analytics_api_base_url=None, analytics_api_token=None, analytics_database_url=None))


@pytest.mark.asyncio
async def test_analytics_outage_does_not_lose_reaction(state_db_path):
    """Avval: o'qish xatosi istisno bo'lib chiqib ketardi va reaksiya yo'qolardi."""
    from app.retry import retry_once
    from app.state import StateStore
    from app.sync_logic import process_reaction
    from tests.test_sync_logic import default_handler_factory, make_config, make_ombor
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(503, json={}) if calls["n"] == 1 else httpx.Response(200, json=ORDER)
    reader = _reader(handler)
    pushed = []
    ombor = make_ombor(default_handler_factory(pushed))
    state = StateStore(state_db_path)
    r = await process_reaction(-100123, 77, make_config(), reader, ombor, state)
    assert r.outcome == "failed" and "Analytics'dan o'qib bo'lmadi" in r.reason
    s = await retry_once(make_config(), reader, ombor, state)   # Analytics tiklandi
    assert s.synced == 1 and pushed[0]["source_id"] == "analytics-order:990"
    state.close()
