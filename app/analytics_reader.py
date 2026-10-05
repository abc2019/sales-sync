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
    # Analytics'ning YAGONA qabul qoidasi (analytics/sales.py ACCEPTED_SALES_STATUSES):
    # o'chirilmagan, tasdiq kutmayapti va review_status qabul qilinganlardan biri.
    accepted: bool = True
    review_status: str = "APPROVED"


class AnalyticsReader(Protocol):
    async def fetch_order_by_message(
        self, chat_id: int, message_id: int
    ) -> OrderRecord | None: ...


# Analytics bilan AYNAN bir xil (shohona-ai-analytics: analytics/sales.py).
# Ombor'dan faqat Analytics "qabul qilingan sotuv" deb hisoblagan buyurtma ayiriladi.
ACCEPTED_SALES_STATUSES = frozenset({"AUTO_APPROVED", "APPROVED", "CONFIRMED"})


def is_accepted_sale(needs_confirmation, review_status: str | None) -> bool:
    return (not needs_confirmation) and str(review_status or "").upper() in ACCEPTED_SALES_STATUSES


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
                SELECT id, is_deleted, needs_confirmation, review_status FROM orders
                WHERE telegram_chat_id = $1 AND telegram_message_id = $2
                """,
                chat_id,
                message_id,
            )
            if order_row is None or order_row["is_deleted"]:
                return None

            item_rows = await conn.fetch(
                "SELECT product, units_total FROM order_items WHERE order_id = $1",
                order_row["id"],
            )
        finally:
            await conn.close()

        status = str(order_row["review_status"] or "").upper()
        return OrderRecord(
            accepted=is_accepted_sale(order_row["needs_confirmation"], status),
            review_status=status or "NONE",
            order_id=order_row["id"],
            items=tuple(
                OrderItem(product_code=r["product"], units_total=r["units_total"])
                for r in item_rows
            ),
        )


class ApiAnalyticsReader:
    """Analytics ichki API orqali (ERP kontrakti, Analytics #57) - bazaga
    to'g'ridan-to'g'ri ulanmaydi. Qabul holatini Analytics o'zi hisoblaydi."""

    def __init__(self, base_url: str, token: str, transport=None):
        base = base_url.strip().rstrip("/")
        self.base_url = base if "://" in base else "https://" + base
        self.token = token
        self.transport = transport

    async def fetch_order_by_message(self, chat_id: int, message_id: int) -> OrderRecord | None:
        import httpx
        async with httpx.AsyncClient(timeout=15, transport=self.transport) as client:
            resp = await client.get(
                f"{self.base_url}/internal/orders/by-message",
                params={"chat_id": chat_id, "message_id": message_id},
                headers={"Authorization": f"Bearer {self.token}"},
            )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()  # 401/5xx - xato: avtomatik qayta urinish ushlaydi
        data = resp.json()
        if data.get("is_deleted"):
            return None
        return OrderRecord(
            order_id=int(data["order_id"]),
            items=tuple(OrderItem(product_code=i["product"], units_total=int(i["units_total"]))
                        for i in data.get("items") or []),
            accepted=bool(data["accepted"]),
            review_status=str(data.get("review_status") or "NONE"),
        )


def build_reader(config) -> "AnalyticsReader":
    """API sozlangan bo'lsa - API (tavsiya); aks holda - bazaga to'g'ridan-to'g'ri (o'tish davri)."""
    if getattr(config, "analytics_api_base_url", None) and getattr(config, "analytics_api_token", None):
        return ApiAnalyticsReader(config.analytics_api_base_url, config.analytics_api_token)
    if not config.analytics_database_url:
        raise RuntimeError("ANALYTICS_API_BASE_URL+ANALYTICS_API_TOKEN yoki ANALYTICS_DATABASE_URL kerak")
    return PostgresAnalyticsReader(config.analytics_database_url)
