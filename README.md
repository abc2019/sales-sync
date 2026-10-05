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
4. **Agar topilsa** — Analytics'ning qat'iy mahsulot kodlari (`palov`,
   `dimlama` va h.k.) **o'zicha** Ombor'ga yuboriladi
   (`POST /sales-shipments/by-mapping`, `system=analytics`). Kodlarni
   mahsulotga **Ombor o'zi** aylantiradi — xarita Ombor'da (ERP mahsulot
   ma'lumotnomasi, Ombor #67), bu servisda saqlanmaydi. Ombor'da bog'lanmagan
   kod — hech narsa yozilmaydi, `NEEDS_REVIEW` + OWNER'ga xabar.
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
- ~~`PRODUCT_CODE_MAP`~~ — **eskirgan**. Xarita endi Ombor'da: Ombor botida
  ⚙️ Sozlamalar → 🔗 Mahsulot kodlari (yoki `PUT /product-mappings/analytics/{kod}`).
  Ko'chirish: eski JSON qiymatini o'sha bo'limga bir marta yuboring, keyin
  o'zgaruvchini o'chiring (qolsa — ishga tushishda ogohlantirish).

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

## Avtomatik qayta urinish

Ombor'ga yozilmagan buyurtmalar (`FAILED` — Ombor ishlamadi/tarmoq;
`NEEDS_REVIEW` — masalan Ombor'da bog'lanmagan kod) har
`SALES_RETRY_INTERVAL_MINUTES` (standart **10**, `0` — o'chiq) daqiqada **o'zi**
qayta tekshiriladi — reaksiyani qayta qo'yish shart emas. Ombor tiklansa yoki
owner kodni Ombor botida bog'lasa (🔗 Mahsulot kodlari) — buyurtma o'zi yoziladi
va OWNER'ga ✅ keladi.

- Birinchi ko'rilganidan `SALES_RETRY_MAX_AGE_DAYS` (standart **7**) kun o'tgan buyurtmalar endi urinilmaydi.
- Bir aylanishda ko'pi bilan 50 ta.
- Takror yozuv yo'q (Ombor `source_id` idempotent); sabab o'zgarmasa — takroriy xabar yo'q.
- Holat fayli Volume'da bo'lishi kerak (`STATE_DATABASE_PATH`) — aks holda deploy'da ro'yxat yo'qoladi.

## Qabul qilingan sotuv (Analytics bilan yagona qoida)

Ombor'dan faqat Analytics **qabul qilgan** buyurtma ayiriladi — Analytics'ning o'z
qoidasi bilan aynan bir xil (`analytics/sales.py`): o'chirilmagan, `needs_confirmation = false`
va `review_status ∈ {AUTO_APPROVED, APPROVED, CONFIRMED}`.

- Hali qabul qilinmagan (masalan `PENDING`) — Ombor'ga yuborilmaydi, OWNER'ga xabar ham
  yo'q (bu Analytics ko'rib chiqish navbatining ishi). Analytics'da tasdiqlangach —
  avtomatik qayta urinish (10 daqiqa ichida) o'zi yozadi.
- O'chirilgan buyurtma — "buyurtma emas".

## Analytics'ga ulanish: ichki API (ERP kontrakti)

`ANALYTICS_API_BASE_URL` va `ANALYTICS_API_TOKEN` berilgan bo'lsa — buyurtma Analytics'ning
ichki faqat-o'qish API'sidan olinadi (`GET /internal/orders/by-message`, Analytics #57);
qabul holatini (`accepted`) Analytics o'zi hisoblaydi. Berilmasa — eski yo'l
(`ANALYTICS_DATABASE_URL`, bazaga to'g'ridan-to'g'ri) — o'tish davri uchun.

Analytics (API yoki baza) vaqtincha ishlamasa — reaksiya **yo'qolmaydi**: `FAILED` bo'lib
yoziladi va avtomatik qayta urinish (10 daqiqa) Analytics tiklangach o'zi yozadi.
