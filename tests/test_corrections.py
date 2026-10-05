"""C: Analytics'da buyurtma o'zgarsa/o'chirilsa - Ombor tuzatiladi; bonus ham ayiriladi."""
import json

import httpx
import pytest

from app.analytics_reader import OrderItem, OrderRecord
from app.corrections import correct_once, diff, target_fingerprint
from app.state import StateStore
from app.sync_logic import process_reaction
from tests.test_sync_logic import make_config, make_ombor

CHAT = -100123


class Analytics:
    def __init__(self):
        self.orders = {}

    def set(self, message_id, order_id, items, bonus=(), accepted=True):
        self.orders[message_id] = OrderRecord(
            order_id=order_id, items=tuple(OrderItem(p, q) for p, q in items),
            bonus_items=tuple(OrderItem(p, q) for p, q in bonus), accepted=accepted,
            review_status="APPROVED" if accepted else "PENDING")

    async def fetch_order_by_message(self, chat_id, message_id):
        return self.orders.get(message_id)


def ombor_recorder(calls, unmapped=()):
    def handler(request):
        body = json.loads(request.content) if request.content else {}
        if request.url.path in ("/sales-shipments/by-mapping", "/sales-shipments/corrections/by-mapping"):
            bad = sorted({i["code"] for i in body["items"] if i["code"] in unmapped})
            if bad:
                return httpx.Response(422, json={"detail": {"message": "m", "unmapped_codes": bad}})
            calls.append((request.url.path.rsplit("/", 2)[-2], body))
            return httpx.Response(201, json={"id": "e"})
        if request.url.path == "/system-alerts":
            calls.append(("alert", body))
            return httpx.Response(201, json={})
        return httpx.Response(404, json={})
    return make_ombor(handler)


def test_diff_and_fingerprint():
    assert diff({"palov": 24}, {"palov": 20, "non": 2}) == {"non": 2, "palov": -4}
    assert diff({"palov": 24}, {}) == {"palov": -24}
    assert diff({"palov": 24}, {"palov": 24}) == {}
    assert target_fingerprint({}) != target_fingerprint({"palov": 1})


@pytest.mark.asyncio
async def test_initial_push_includes_bonus(state_db_path):
    a, calls = Analytics(), []
    a.set(77, 990, [("palov", 24)], bonus=[("non", 2), ("palov", 1)])
    state = StateStore(state_db_path)
    r = await process_reaction(CHAT, 77, make_config(), a, ombor_recorder(calls), state)
    assert r.outcome == "synced"
    assert calls[0][1]["items"] == [{"code": "non", "quantity": "2"}, {"code": "palov", "quantity": "25"}]
    assert state.list_watched(max_age_days=3) == [(f"{CHAT}:77", 990, {"non": 2, "palov": 25})]
    state.close()


@pytest.mark.asyncio
async def test_change_then_cancel_are_corrected_once(state_db_path):
    a, calls = Analytics(), []
    ombor = ombor_recorder(calls)
    a.set(77, 990, [("palov", 24)])
    state = StateStore(state_db_path)
    await process_reaction(CHAT, 77, make_config(), a, ombor, state)
    calls.clear()

    assert (await correct_once(a, ombor, state)).corrected == 0  # o'zgarish yo'q - jim
    assert calls == []

    a.set(77, 990, [("palov", 20)], bonus=[("non", 2)])  # tuzatildi + bonus javobi
    s = await correct_once(a, ombor, state)
    assert s.corrected == 1
    kind, body = calls[0]
    assert kind == "corrections" and body["items"] == [{"code": "non", "quantity": "2"}, {"code": "palov", "quantity": "-4"}]
    assert body["source_id"].startswith("analytics-order:990:rev:")
    assert calls[1][0] == "alert" and "Ombor tuzatildi: non +2, palov -4" in calls[1][1]["message"]
    calls.clear()
    assert (await correct_once(a, ombor, state)).corrected == 0  # takror yo'q

    del a.orders[77]  # Analytics'da o'chirildi
    await correct_once(a, ombor, state)
    assert calls[0][1]["items"] == [{"code": "non", "quantity": "-2"}, {"code": "palov", "quantity": "-20"}]
    assert "bekor qilindi" in calls[1][1]["message"]
    state.close()


@pytest.mark.asyncio
async def test_unaccepted_after_sync_returns_stock(state_db_path):
    a, calls = Analytics(), []
    ombor = ombor_recorder(calls)
    a.set(78, 991, [("palov", 10)])
    state = StateStore(state_db_path)
    await process_reaction(CHAT, 78, make_config(), a, ombor, state)
    calls.clear()
    a.set(78, 991, [("palov", 10)], accepted=False)  # qayta ko'rib chiqishga qaytdi
    await correct_once(a, ombor, state)
    assert calls[0][1]["items"] == [{"code": "palov", "quantity": "-10"}]
    state.close()


@pytest.mark.asyncio
async def test_failure_alerts_and_retries(state_db_path):
    a, calls = Analytics(), []
    a.set(79, 992, [("palov", 5)])
    state = StateStore(state_db_path)
    await process_reaction(CHAT, 79, make_config(), a, ombor_recorder(calls), state)
    calls.clear()
    a.set(79, 992, [("palov", 5)], bonus=[("yangi_mahsulot", 1)])
    s = await correct_once(a, ombor_recorder(calls, unmapped=("yangi_mahsulot",)), state)
    assert s.failed == 1 and calls[-1][0] == "alert" and "bog'lanmagan kod: yangi_mahsulot" in calls[-1][1]["message"]
    calls.clear()
    s = await correct_once(a, ombor_recorder(calls), state)  # xarita to'ldirildi
    assert s.corrected == 1 and calls[0][1]["items"] == [{"code": "yangi_mahsulot", "quantity": "1"}]
    state.close()


@pytest.mark.asyncio
async def test_analytics_outage_skips_safely(state_db_path):
    class Down(Analytics):
        async def fetch_order_by_message(self, chat_id, message_id):
            raise httpx.ConnectError("x")
    a, calls = Analytics(), []
    a.set(80, 993, [("palov", 1)])
    state = StateStore(state_db_path)
    await process_reaction(CHAT, 80, make_config(), a, ombor_recorder(calls), state)
    calls.clear()
    s = await correct_once(Down(), ombor_recorder(calls), state)
    assert s.failed == 1 and calls == []  # hech narsa qaytarilmaydi (taxmin yo'q)
    state.close()
