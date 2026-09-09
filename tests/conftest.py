import pytest

from app.analytics_reader import OrderItem, OrderRecord


class FakeAnalyticsReader:
    """Testlarda Analytics'ning Postgres bazasini almashtiradi."""

    def __init__(self):
        self._orders: dict[tuple[int, int], OrderRecord] = {}

    def add_order(self, *, chat_id: int, message_id: int, order_id: int, items: list[tuple[str, int]]):
        self._orders[(chat_id, message_id)] = OrderRecord(
            order_id=order_id,
            items=tuple(OrderItem(product_code=p, units_total=q) for p, q in items),
        )

    async def fetch_order_by_message(self, chat_id: int, message_id: int) -> OrderRecord | None:
        return self._orders.get((chat_id, message_id))


@pytest.fixture()
def analytics():
    return FakeAnalyticsReader()


@pytest.fixture()
def state_db_path(tmp_path):
    return str(tmp_path / "state_test.db")
