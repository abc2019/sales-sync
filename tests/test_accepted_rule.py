"""Analytics bilan yagona qabul qoidasi: faqat qabul qilingan sotuv Ombor'ga."""
import pytest

from app.analytics_reader import ACCEPTED_SALES_STATUSES, is_accepted_sale
from app.retry import retry_once
from app.state import StateStore
from app.sync_logic import process_reaction
from tests.test_sync_logic import default_handler_factory, make_config, make_ombor


@pytest.mark.parametrize("needs_confirmation,status,expected", [
    (False, "AUTO_APPROVED", True), (False, "APPROVED", True), (False, "confirmed", True),
    (False, "NONE", False), (False, "PENDING", False), (False, "REJECTED", False), (False, None, False),
    (True, "APPROVED", False),   # tasdiq kutmoqda - qabul emas
])
def test_rule_matches_analytics(needs_confirmation, status, expected):
    assert is_accepted_sale(needs_confirmation, status) is expected


def test_same_statuses_as_analytics():
    # shohona-ai-analytics: analytics/sales.py ACCEPTED_SALES_STATUSES
    assert ACCEPTED_SALES_STATUSES == {"AUTO_APPROVED", "APPROVED", "CONFIRMED"}


@pytest.mark.asyncio
async def test_unaccepted_order_waits_then_syncs_after_approval(analytics, state_db_path):
    pushed = []
    analytics.add_order(chat_id=-100123, message_id=90, order_id=990, items=[("palov", 2)], review_status="PENDING")
    ombor = make_ombor(default_handler_factory(pushed))
    state = StateStore(state_db_path)
    config = make_config()
    r = await process_reaction(-100123, 90, config, analytics, ombor, state)
    assert r.outcome == "needs_review" and r.notify is False and "PENDING" in r.reason
    assert pushed == []
    # Analytics'da tasdiqlandi -> avtomatik qayta urinish o'zi yozadi
    analytics.add_order(chat_id=-100123, message_id=90, order_id=990, items=[("palov", 2)], review_status="APPROVED")
    s = await retry_once(config, analytics, ombor, state)
    assert s.synced == 1 and pushed[0]["source_id"] == "analytics-order:990"
    state.close()


@pytest.mark.asyncio
async def test_needs_confirmation_blocks_even_if_reviewed_before(analytics, state_db_path):
    """Eski qoida (reviewed_by IS NOT NULL) bunday buyurtmani o'tkazib yuborardi."""
    pushed = []
    analytics.add_order(chat_id=-100123, message_id=91, order_id=991, items=[("palov", 1)],
                        review_status="APPROVED", needs_confirmation=True)
    state = StateStore(state_db_path)
    r = await process_reaction(-100123, 91, make_config(), analytics, make_ombor(default_handler_factory(pushed)), state)
    assert r.outcome == "needs_review" and pushed == []
    state.close()
