import httpx
import pytest

from erp_bridge_kit import ModuleClient, OmborBridgeClient

from app.config import Config
from app.state import StateStore
from app.sync_logic import process_reaction


def make_config(product_code_map=None, ombor_base_url="http://ombor.test"):
    return Config(
        sales_bot_token="dummy",
        sales_group_chat_id=-100123,
        analytics_database_url="postgresql://u:p@localhost/db",
        ombor_api_base_url=ombor_base_url,
        ombor_actor_name="sales-sync-test",
        state_database_path=":memory:",
        product_code_map=product_code_map or {"palov": ["PALOV_ANDIJON"], "dimlama": ["DIMLAMA_1KG"]},
    )


def make_ombor(handler):
    client = ModuleClient("http://ombor.test", transport=httpx.MockTransport(handler))
    return OmborBridgeClient(client)


# Ombor'dagi xarita (ERP mahsulot ma'lumotnomasi) - soxta Ombor shu bo'yicha
# by-mapping so'rovini kengaytiradi va eski ko'rinishda (Ombor kodlari) yozadi.
OMBOR_MAPPING = {
    "palov": ["PALOV_ANDIJON"],
    "dimlama": ["DIMLAMA_1KG"],
    "qozon_kabob": ["QOZON_KABOB_GOSHT", "QOZON_KABOB_FRI"],
}


def default_handler_factory(pushed: list, mapping: dict | None = None):
    mapping = OMBOR_MAPPING if mapping is None else mapping

    def handler(request: httpx.Request) -> httpx.Response:
        import json
        if request.url.path == "/sales-shipments/by-mapping":
            body = json.loads(request.content)
            assert body["system"] == "analytics"
            missing = sorted({i["code"] for i in body["items"] if i["code"] not in mapping})
            if missing:
                return httpx.Response(422, json={"detail": {"message": "m", "unmapped_codes": missing}})
            pushed.append({
                "source_id": body["source_id"], "order_reference": body["order_reference"],
                "items": [{"finished_product_external_code": code, "quantity": i["quantity"], "unit": "dona"}
                          for i in body["items"] for code in mapping[i["code"]]],
            })
            return httpx.Response(201, json={"id": "shipment-1", "duplicate": False})
        return httpx.Response(404, json={"detail": "not found"})
    return handler


@pytest.mark.asyncio
async def test_ordinary_conversation_is_ignored(analytics, state_db_path):
    # Analytics'da bu xabar bo'yicha order yo'q - demak oddiy suhbat
    pushed = []
    ombor = make_ombor(default_handler_factory(pushed))
    state = StateStore(state_db_path)
    config = make_config()

    result = await process_reaction(-100123, 555, config, analytics, ombor, state)

    assert result.outcome == "not_an_order"
    assert pushed == []
    state.close()


@pytest.mark.asyncio
async def test_matched_order_pushed_to_ombor(analytics, state_db_path):
    analytics.add_order(chat_id=-100123, message_id=10, order_id=555, items=[("palov", 96)])
    pushed = []
    ombor = make_ombor(default_handler_factory(pushed))
    state = StateStore(state_db_path)
    config = make_config()

    result = await process_reaction(-100123, 10, config, analytics, ombor, state)

    assert result.outcome == "synced"
    assert pushed[0]["order_reference"] == "555"
    assert pushed[0]["items"] == [
        {"finished_product_external_code": "PALOV_ANDIJON", "quantity": "96", "unit": "dona"}
    ]
    assert pushed[0]["source_id"] == "analytics-order:555"
    state.close()


@pytest.mark.asyncio
async def test_split_product_creates_two_ombor_lines(analytics, state_db_path):
    # "Qozon kabob" kabi taomlar bitta buyurtma birligi uchun 2 ta alohida
    # bankaga (Ombor mahsuloti) bo'linadi - har biriga bir xil miqdor.
    analytics.add_order(chat_id=-100123, message_id=30, order_id=800, items=[("qozon_kabob", 2)])
    pushed = []
    ombor = make_ombor(default_handler_factory(pushed))
    state = StateStore(state_db_path)
    config = make_config(
        product_code_map={"qozon_kabob": ["QOZON_KABOB_GOSHT", "QOZON_KABOB_FRI"]}
    )

    result = await process_reaction(-100123, 30, config, analytics, ombor, state)

    assert result.outcome == "synced"
    assert len(pushed[0]["items"]) == 2
    codes = {item["finished_product_external_code"] for item in pushed[0]["items"]}
    assert codes == {"QOZON_KABOB_GOSHT", "QOZON_KABOB_FRI"}
    # Ikkalasi ham bir xil miqdorda (har bir buyurtma birligi har bir
    # komponentdan bittadan talab qiladi)
    assert all(item["quantity"] == "2" for item in pushed[0]["items"])
    state.close()


@pytest.mark.asyncio
async def test_multi_item_order_all_included(analytics, state_db_path):
    analytics.add_order(
        chat_id=-100123, message_id=11, order_id=556,
        items=[("palov", 96), ("dimlama", 40)],
    )
    pushed = []
    ombor = make_ombor(default_handler_factory(pushed))
    state = StateStore(state_db_path)
    config = make_config()

    result = await process_reaction(-100123, 11, config, analytics, ombor, state)

    assert result.outcome == "synced"
    assert len(pushed[0]["items"]) == 2
    state.close()


@pytest.mark.asyncio
async def test_unmapped_product_code_needs_review(analytics, state_db_path):
    analytics.add_order(chat_id=-100123, message_id=12, order_id=557, items=[("nuxatshorak", 10)])
    pushed = []
    ombor = make_ombor(default_handler_factory(pushed))
    state = StateStore(state_db_path)
    config = make_config()  # "nuxatshorak" xaritada yo'q

    result = await process_reaction(-100123, 12, config, analytics, ombor, state)

    assert result.outcome == "needs_review"
    assert "nuxatshorak" in result.reason
    assert pushed == []
    assert len(state.list_needs_review()) == 1
    state.close()


@pytest.mark.asyncio
async def test_empty_order_needs_review(analytics, state_db_path):
    analytics.add_order(chat_id=-100123, message_id=13, order_id=558, items=[])
    ombor = make_ombor(default_handler_factory([]))
    state = StateStore(state_db_path)
    config = make_config()

    result = await process_reaction(-100123, 13, config, analytics, ombor, state)
    assert result.outcome == "needs_review"
    state.close()


@pytest.mark.asyncio
async def test_ombor_failure_marks_failed_and_retries(analytics, state_db_path):
    analytics.add_order(chat_id=-100123, message_id=14, order_id=559, items=[("palov", 24)])

    def failing_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal error")

    ombor = make_ombor(failing_handler)
    state = StateStore(state_db_path)
    config = make_config()

    result = await process_reaction(-100123, 14, config, analytics, ombor, state)
    assert result.outcome == "failed"
    assert state.get_synced_keys() == set()

    pushed = []
    ombor2 = make_ombor(default_handler_factory(pushed))
    result2 = await process_reaction(-100123, 14, config, analytics, ombor2, state)
    assert result2.outcome == "synced"
    assert len(pushed) == 1
    state.close()


@pytest.mark.asyncio
async def test_already_synced_not_reprocessed(analytics, state_db_path):
    analytics.add_order(chat_id=-100123, message_id=15, order_id=560, items=[("palov", 24)])
    pushed = []
    ombor = make_ombor(default_handler_factory(pushed))
    state = StateStore(state_db_path)
    config = make_config()

    result1 = await process_reaction(-100123, 15, config, analytics, ombor, state)
    assert result1.outcome == "synced"

    result2 = await process_reaction(-100123, 15, config, analytics, ombor, state)
    assert result2.outcome == "already_synced"
    assert len(pushed) == 1  # ikkinchi marta push qilinmadi
    state.close()


@pytest.mark.asyncio
async def test_ombor_not_configured_is_safe_noop(analytics, state_db_path):
    analytics.add_order(chat_id=-100123, message_id=16, order_id=561, items=[("palov", 24)])
    ombor = OmborBridgeClient(ModuleClient(None))
    state = StateStore(state_db_path)
    config = make_config(ombor_base_url=None)

    result = await process_reaction(-100123, 16, config, analytics, ombor, state)
    assert result.outcome == "ombor_not_configured"
    state.close()


@pytest.mark.asyncio
async def test_second_reaction_on_different_order_processed_independently(analytics, state_db_path):
    analytics.add_order(chat_id=-100123, message_id=20, order_id=700, items=[("palov", 24)])
    analytics.add_order(chat_id=-100123, message_id=21, order_id=701, items=[("dimlama", 12)])
    pushed = []
    ombor = make_ombor(default_handler_factory(pushed))
    state = StateStore(state_db_path)
    config = make_config()

    r1 = await process_reaction(-100123, 20, config, analytics, ombor, state)
    r2 = await process_reaction(-100123, 21, config, analytics, ombor, state)
    assert r1.outcome == "synced"
    assert r2.outcome == "synced"
    assert len(pushed) == 2
    state.close()


# --- ERP: xarita Ombor'da (POST /sales-shipments/by-mapping) ---
@pytest.mark.asyncio
async def test_sends_analytics_codes_not_ombor_codes(analytics, state_db_path):
    import json
    seen = []

    def handler(request):
        seen.append((request.url.path, json.loads(request.content)))
        return httpx.Response(201, json={"id": "s", "duplicate": False})

    analytics.add_order(chat_id=-100123, message_id=70, order_id=970, items=[("palov", 2), ("qozon_kabob", 1)])
    state = StateStore(state_db_path)
    result = await process_reaction(-100123, 70, make_config(product_code_map={}), analytics,
                                    make_ombor(handler), state)
    state.close()
    assert result.outcome == "synced"
    path, body = seen[0]
    assert path == "/sales-shipments/by-mapping"
    assert body["system"] == "analytics" and body["source_id"] == "analytics-order:970"
    assert body["items"] == [{"code": "palov", "quantity": "2"}, {"code": "qozon_kabob", "quantity": "1"}]


@pytest.mark.asyncio
async def test_unmapped_in_ombor_becomes_needs_review(analytics, state_db_path):
    pushed = []
    analytics.add_order(chat_id=-100123, message_id=71, order_id=971, items=[("palov", 1), ("somsa", 2)])
    state = StateStore(state_db_path)
    result = await process_reaction(-100123, 71, make_config(), analytics,
                                    make_ombor(default_handler_factory(pushed)), state)
    assert result.outcome == "needs_review" and result.notify is True
    assert result.reason == "Ombor'da bog'lanmagan kod: somsa"
    assert pushed == []  # hech narsa yozilmadi
    assert len(state.list_needs_review()) == 1
    state.close()
