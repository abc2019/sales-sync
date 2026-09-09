# sales-sync

Analytics (buyurtma parser) modulidagi buyurtmalarni — xodim Telegram
guruhida buyurtma xabariga **istalgan reaksiya** bosgach — Ombor moduliga
(`POST /sales-shipments/by-code`, W5) avtomatik yuboruvchi kichik ko'prik
xizmati.

To'liq kontekst: `abc2019/inventory` repo'sidagi
`docs/erp_integration_plan.md` (bo'lim 5.5).

**Analytics kodiga hech qanday o'zgarish kiritilmagan** — bu xizmat
o'zining alohida Telegram bot tokeni bilan guruhga a'zo bo'ladi va
Analytics'ning Postgres bazasiga faqat o'qish huquqi bilan ulanadi.

## Qanday ishlaydi

1. `app/bot.py` — alohida, yangi Telegram bot orqali buyurtma guruhidagi
   **istalgan xabarga, istalgan kishidan kelgan istalgan reaksiyani**
   kuzatadi (aiogram'ning `message_reaction` hodisasi).
2. Reaksiya kelganda, Analytics'ning Postgres bazasidan (**faqat o'qish**)
   shu xabar (`telegram_chat_id`+`telegram_message_id`) bo'yicha
   `orders`/`order_items`ni so'raydi.
3. **Agar order topilmasa** — bu xabar oddiy suhbat edi (Analytics'ning
   o'z parser'i uni buyurtma deb tanimagan). Hech narsa qilinmaydi. Bu
   bilan "buyurtmami yoki suhbatmi" ajratish muammosi Analytics'ning
   mavjud parser mantig'iga tayanib, qo'shimcha kodsiz hal bo'ladi.
4. **Agar topilsa** — Analytics'ning qat'iy mahsulot kodlarini
   (`palov`, `dimlama` va h.k. — atigi ~17 ta, erkin matn emas)
   `PRODUCT_CODE_MAP` orqali Ombor'ning `external_code`iga aylantiradi.
   Xaritada yo'q kod — avtomatik yozilmaydi, mahalliy holatda
   `NEEDS_REVIEW` sifatida saqlanadi.
5. Barcha qatorlar mos kelsa → Ombor'ning `POST /sales-shipments/by-code`
   (W5)ga `source_id=analytics-order:{order_id}` bilan yuboriladi
   (idempotent).
6. Ombor vaqtincha ishlamasa → `FAILED`, keyingi reaksiyada (yoki qayta
   ishga tushirilganda) qayta uriniladi. Faqat `SYNCED` chetlab o'tiladi.

## Sozlash

`.env.example`ga qarang. Eng muhimlari:

- `SALES_BOT_TOKEN` — @BotFather'dan yangi, alohida bot yarating va
  buyurtma guruhiga qo'shing (reaksiyalarni ko'rish uchun kamida oddiy
  a'zo bo'lishi kifoya, lekin guruh sozlamalariga qarab admin kerak
  bo'lishi mumkin).
- `ANALYTICS_DATABASE_URL` — Analytics'ning Postgres'ida yangi,
  **faqat SELECT** huquqli foydalanuvchi yaratib, shu bilan hosil
  qilingan ulanish satri (tavsiya etiladi):
  ```sql
  CREATE USER sales_sync_ro WITH PASSWORD '...';
  GRANT CONNECT ON DATABASE analytics_db TO sales_sync_ro;
  GRANT USAGE ON SCHEMA public TO sales_sync_ro;
  GRANT SELECT ON orders, order_items TO sales_sync_ro;
  ```
- `PRODUCT_CODE_MAP` — Analytics'ning qat'iy mahsulot kodi -> Ombor
  `external_code` xaritasi (JSON). Ombor'da mavjud bo'lmagan mahsulotlar
  uchun kod kiritmang — avtomatik review'ga tushadi, xato yozilmaydi.

## Ishga tushirish

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # to'ldiring
python -m app.bot
```

## Testlar

```bash
pip install -r requirements.txt
pytest -q
```

15 test: orchestratsiya (oddiy suhbat e'tiborsiz qoldiriladi, mos kelgan
buyurtma push qilinadi, ko'p qatorli buyurtma, xaritada yo'q kod
review'ga tushadi, bo'sh buyurtma review'ga tushadi, Ombor xatosidan
keyin qayta tiklanish, takroriy qayta ishlanmaslik, Ombor sozlanmaganda
xavfsiz to'xtash, bir nechta buyurtma mustaqil ishlanishi), holat bazasi.
Barchasi soxta (fake) Analytics reader bilan, tarmoqqa chiqmasdan.
