"""
Analytics'ning Postgres bazasidan (FAQAT O'QISH) buyurtmani topadi.
Analytics'ning kodiga yoki bazasiga hech qanday YOZUV qilinmaydi.

MUHIM: `orders` jadvalida "jo'natildi" holati yo'q — bu modul faqat
Analytics parser TASDIQLAGAN (yoki tasdiqlash shart bo'lmagan) buyurtmalarni
qaytaradi. Xabar guruhda oddiy suhbat bo'lsa (buyurtma emas), Analytics uni
umuman `orders`ga yozmagan bo'ladi — shu holda bu funksiya None qaytaradi,
va sales-sync hech narsa qilmaydi (bu — "buyurtmami yoki suhbatmi" ajratish
Analytics'ning o'z parser'i tomonidan allaqachon hal qilingan degani).
"""
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class OrderItem:
    product_code: str  # Analytics'ning qat'iy kodi (masalan "palov")
    units_total: int  # allaqachon baza birlikka (dona) aylantirilgan umumiy son


@dataclass(frozen=True)
class OrderRecord:
    order_id: int
    items: tuple[OrderItem, ...]


class AnalyticsReader(Protocol):
    async def fetch_order_by_message(
        self, chat_id: int, message_id: int
    ) -> OrderRecord | None: ...


class PostgresAnalyticsReader:
    """Haqiqiy, faqat-o'qish Postgres implementatsiyasi."""

    def __init__(self, database_url: str):
        self.database_url = database_url

    async def fetch_order_by_message(
        self, chat_id: int, message_id: int
    ) -> OrderRecord | None:
        import asyncpg  # local import: testlarda asyncpg/tarmoq talab qilinmasin

        conn = await asyncpg.connect(self.database_url)
        try:
            order_row = await conn.fetchrow(
                """
                SELECT id FROM orders
                WHERE telegram_chat_id = $1 AND telegram_message_id = $2
                  AND is_deleted = false
                  AND (needs_confirmation = false OR reviewed_by IS NOT NULL)
                """,
                chat_id,
                message_id,
            )
            if order_row is None:
                return None

            item_rows = await conn.fetch(
                "SELECT product, units_total FROM order_items WHERE order_id = $1",
                order_row["id"],
            )
        finally:
            await conn.close()

        return OrderRecord(
            order_id=order_row["id"],
            items=tuple(
                OrderItem(product_code=r["product"], units_total=r["units_total"])
                for r in item_rows
            ),
        )
