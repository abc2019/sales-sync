"""'Ko'rib chiqish kerak' buyurtmalar OWNER'ga xabar qilinadi (bir marta)."""
import pytest

from app.bot import notify_review
from app.config import parse_chat_ids
from app.state import StateStore
from app.sync_logic import ReactionResult, build_review_message, message_link, process_reaction
from tests.test_sync_logic import default_handler_factory, make_config, make_ombor


class FakeBot:
    def __init__(self, fail_for=()):
        self.sent = []
        self.fail_for = set(fail_for)

    async def send_message(self, chat_id, text, **kwargs):
        if chat_id in self.fail_for:
            raise RuntimeError("Forbidden: bot can't initiate conversation with a user")
        self.sent.append((chat_id, text))


def _config(**overrides):
    cfg = make_config(product_code_map={"palov": ["PALOV_ANDIJON"]})
    from dataclasses import replace
    return replace(cfg, **overrides)


@pytest.mark.asyncio
async def test_unmapped_code_notifies_once_then_again_only_if_reason_changes(analytics, state_db_path):
    analytics.add_order(chat_id=-100123, message_id=55, order_id=901, items=[("somsa", 3)])
    state = StateStore(state_db_path)
    config = _config()
    ombor = make_ombor(default_handler_factory([]))

    first = await process_reaction(-100123, 55, config, analytics, ombor, state)
    assert first.outcome == "needs_review" and first.notify is True
    assert first.order_id == 901 and "somsa" in first.reason

    again = await process_reaction(-100123, 55, config, analytics, ombor, state)  # qayta reaksiya
    assert again.outcome == "needs_review" and again.notify is False  # spam yo'q

    analytics.add_order(chat_id=-100123, message_id=55, order_id=901, items=[("somsa", 3), ("manti", 1)])
    changed = await process_reaction(-100123, 55, config, analytics, ombor, state)
    assert changed.notify is True  # sabab o'zgardi - yana xabar
    state.close()


@pytest.mark.asyncio
async def test_empty_order_carries_order_id_and_notifies(analytics, state_db_path):
    analytics.add_order(chat_id=-100123, message_id=56, order_id=902, items=[])
    state = StateStore(state_db_path)
    result = await process_reaction(-100123, 56, _config(), analytics, make_ombor(default_handler_factory([])), state)
    assert result.outcome == "needs_review" and result.notify and result.order_id == 902
    state.close()


def test_review_message_contains_reason_order_and_link():
    result = ReactionResult(outcome="needs_review", reason="PRODUCT_CODE_MAP'da yo'q: somsa", order_id=901)
    text = build_review_message(-1001234567890, 55, result)
    assert "Sabab: PRODUCT_CODE_MAP'da yo'q: somsa" in text
    assert "Buyurtma: #901" in text
    assert "https://t.me/c/1234567890/55" in text
    assert message_link(-4567, 9) is None  # oddiy guruh - havola yo'q
    assert "Xabar ID: 9" in build_review_message(-4567, 9, result)


@pytest.mark.asyncio
async def test_notify_review_sends_to_all_and_survives_failures():
    bot = FakeBot(fail_for={222})
    result = ReactionResult(outcome="needs_review", reason="x", order_id=1)
    delivered = await notify_review(bot, _config(review_notify_chat_ids=(111, 222, 333)), -100123, 5, result)
    assert delivered == 2
    assert [c for c, _ in bot.sent] == [111, 333]


@pytest.mark.asyncio
async def test_notify_review_without_targets_only_logs():
    bot = FakeBot()
    result = ReactionResult(outcome="needs_review", reason="x")
    assert await notify_review(bot, _config(), -100123, 5, result) == 0
    assert bot.sent == []


def test_parse_chat_ids():
    assert parse_chat_ids("") == ()
    assert parse_chat_ids(" 111, 222 ;333,") == (111, 222, 333)
    with pytest.raises(RuntimeError):
        parse_chat_ids("111,abc")


# --- Yozilmagan sotuv -> Ombor /system-alerts ---
@pytest.mark.asyncio
async def test_failed_push_alerts_then_recovered_after_retry(analytics, state_db_path):
    import json

    import httpx
    from erp_bridge_kit import ModuleClient, OmborBridgeClient

    from app.sync_logic import alert_for_result
    alerts, fail = [], {"on": True}

    def handler(request):
        if request.url.path == "/system-alerts":
            alerts.append(json.loads(request.content))
            return httpx.Response(201, json={})
        if request.url.path == "/sales-shipments/by-code":
            if fail["on"]:
                return httpx.Response(400, json={"detail": "Yetarli tayyor mahsulot qoldig'i yo'q"})
            return httpx.Response(201, json={"id": "s1"})
        return httpx.Response(404)

    ombor = OmborBridgeClient(ModuleClient("http://ombor.test", transport=httpx.MockTransport(handler)))
    analytics.add_order(chat_id=-100123, message_id=77, order_id=990, items=[("palov", 2)])
    state = StateStore(state_db_path)
    config = _config()

    r1 = await process_reaction(-100123, 77, config, analytics, ombor, state)
    assert r1.outcome == "failed" and r1.order_id == 990
    await alert_for_result(ombor, -100123, 77, r1)
    fail["on"] = False
    r2 = await process_reaction(-100123, 77, config, analytics, ombor, state)  # reaksiya qayta qo'yildi
    assert r2.outcome == "synced"
    await alert_for_result(ombor, -100123, 77, r2)
    state.close()

    assert [(a["key"], a["level"]) for a in alerts] == [
        ("sales-sync:order:990", "warning"), ("sales-sync:order:990", "recovered"),
    ]
    assert "Yetarli tayyor mahsulot" in alerts[0]["message"]
    assert "https://t.me/c/123/77" in alerts[0]["message"]
    assert alerts[0]["source"] == "sales-sync"


@pytest.mark.asyncio
async def test_alert_for_result_ignores_other_outcomes():
    from app.sync_logic import alert_for_result

    class Never:
        is_configured = True

        async def send_system_alert(self, **kw):
            raise AssertionError("chaqirilmasligi kerak")

    await alert_for_result(Never(), -100123, 1, ReactionResult(outcome="needs_review", reason="x", order_id=1))
    await alert_for_result(Never(), -100123, 1, ReactionResult(outcome="not_an_order"))
