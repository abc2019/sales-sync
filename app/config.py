import json
import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Config:
    sales_bot_token: str  # yangi, alohida Telegram bot (faqat reaksiya kuzatish uchun)
    sales_group_chat_id: int  # buyurtma guruhining Telegram chat ID'si
    analytics_database_url: str  # Analytics Postgres'iga FAQAT O'QISH ulanishi
    ombor_api_base_url: str | None
    ombor_actor_name: str
    state_database_path: str
    # Analytics'ning qat'iy mahsulot kodlari (masalan "palov", "mol_tushonka")
    # Ombor'ning external_code'iga xaritasi. Qiymat bitta kod (string) yoki
    # bir nechta kod (list) bo'lishi mumkin — ba'zi taomlar (masalan "Qozon
    # kabob") bitta buyurtma birligi uchun bir nechta ALOHIDA bankaga
    # (Ombor mahsuloti) bo'linadi. Owner tomonidan bir marta to'ldiriladi.
    product_code_map: dict[str, list[str]] = field(default_factory=dict)
    ombor_api_token: str | None = None  # Ombor token auth (ixtiyoriy; docs/auth.md - inventory)


def load_config() -> Config:
    raw_map = os.getenv("PRODUCT_CODE_MAP", "{}")
    try:
        parsed = json.loads(raw_map)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"PRODUCT_CODE_MAP noto'g'ri JSON: {e}")

    # Har bir qiymatni ro'yxatga normallashtiramiz — chaqiruvchi kod bitta
    # ("palov": "PALOV") yoki bir nechta ("qozon_kabob": ["A", "B"]) berishi
    # mumkin, ikkalasi ham qo'llab-quvvatlanadi.
    product_code_map: dict[str, list[str]] = {}
    for key, value in parsed.items():
        if isinstance(value, str):
            product_code_map[key] = [value]
        elif isinstance(value, list):
            product_code_map[key] = value
        else:
            raise RuntimeError(
                f"PRODUCT_CODE_MAP['{key}'] noto'g'ri turda: string yoki list bo'lishi kerak"
            )

    return Config(
        sales_bot_token=os.environ["SALES_BOT_TOKEN"],
        sales_group_chat_id=int(os.environ["SALES_GROUP_CHAT_ID"]),
        analytics_database_url=os.environ["ANALYTICS_DATABASE_URL"],
        ombor_api_base_url=os.getenv("OMBOR_API_BASE_URL", "").strip() or None,
        ombor_actor_name=os.getenv("OMBOR_ACTOR_NAME", "sales-sync"),
        state_database_path=os.getenv("STATE_DATABASE_PATH", "sales_sync_state.db"),
        product_code_map=product_code_map,
        ombor_api_token=os.getenv("OMBOR_API_TOKEN", "").strip() or None,
    )
