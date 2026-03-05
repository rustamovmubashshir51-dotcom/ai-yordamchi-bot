import os
import re
import asyncio
import logging
from datetime import datetime, timedelta, timezone

import aiosqlite
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    CallbackQuery,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

# -------------------- LOGGING --------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("smartyordam")

# -------------------- ENV --------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()

# ADMIN_IDS: "123,456" ko‘rinishida
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "").strip()
ADMIN_IDS = set()
for part in re.split(r"[,\s]+", ADMIN_IDS_RAW):
    part = part.strip()
    if part.isdigit():
        ADMIN_IDS.add(int(part))

# PRO settings
PRO_PRICE_UZS = int(os.getenv("PRO_PRICE_UZS", "10000"))
PAY_CARD = os.getenv("PAY_CARD", "8600 0000 0000 0000").strip()
PAY_OWNER = os.getenv("PAY_OWNER", "Mubashshir").strip()
PRO_DAYS_DEFAULT = int(os.getenv("PRO_DAYS", "30"))

# Daily limit
FREE_DAILY_LIMIT = int(os.getenv("FREE_DAILY_LIMIT", "20"))

DB_PATH = os.getenv("DB_PATH", "bot.db").strip()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN yo‘q. Railway Variables ga BOT_TOKEN qo‘ying.")
if not GROQ_API_KEY:
    log.warning("GROQ_API_KEY yo‘q. AI javoblar ishlamasligi mumkin.")

# -------------------- GROQ (OpenAI-compatible) --------------------
# openai>=1.0
# pip install openai
try:
    from openai import AsyncOpenAI

    groq_client = AsyncOpenAI(
        api_key=GROQ_API_KEY,
        base_url="https://api.groq.com/openai/v1",
    )
except Exception as e:
    groq_client = None
    log.warning(f"OpenAI client init bo‘lmadi: {e}")


# -------------------- KEYBOARDS --------------------
def kb_main() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📚 Matematika"), KeyboardButton(text="🇬🇧 Ingliz tili")],
            [KeyboardButton(text="📝 Essay"), KeyboardButton(text="📖 Konspekt")],
            [KeyboardButton(text="⭐ PRO"), KeyboardButton(text="🎁 Referral")],
            [KeyboardButton(text="📊 Limit"), KeyboardButton(text="🆔 MyID")],
            [KeyboardButton(text="📊 Admin")],
        ],
        resize_keyboard=True
    )


def kb_phone_request() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📞 Telefon yuborish", request_contact=True)]
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )


def ikb_pro_pay() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ To‘ladim", callback_data="pro_paid")],
            [InlineKeyboardButton(text="⬅️ Orqaga", callback_data="back_main")],
        ]
    )


def ikb_admin() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👥 Statistika", callback_data="admin_stats")],
            [InlineKeyboardButton(text="📣 Broadcast", callback_data="admin_broadcast")],
            [InlineKeyboardButton(text="📋 Userlar", callback_data="admin_users")],
        ]
    )


def ikb_admin_approve(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⭐ PRO berish (30 kun)", callback_data=f"admin_grant_pro:{user_id}")],
            [InlineKeyboardButton(text="❌ Rad etish", callback_data=f"admin_reject_pro:{user_id}")],
        ]
    )


# -------------------- DB --------------------
CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    full_name TEXT,
    joined_at TEXT,
    phone TEXT,
    phone_added_at TEXT
);

CREATE TABLE IF NOT EXISTS pro (
    user_id INTEGER PRIMARY KEY,
    pro_until TEXT
);

CREATE TABLE IF NOT EXISTS usage (
    user_id INTEGER,
    day TEXT,
    cnt INTEGER,
    PRIMARY KEY(user_id, day)
);

CREATE TABLE IF NOT EXISTS pro_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    created_at TEXT,
    status TEXT
);
"""

async def db_init():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(CREATE_TABLES_SQL)
        await db.commit()


async def upsert_user(message: Message):
    u = message.from_user
    if not u:
        return
    now = datetime.now(timezone.utc).isoformat()

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO users(user_id, username, full_name, joined_at)
            VALUES(?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                full_name=excluded.full_name
            """,
            (u.id, u.username or "", u.full_name or "", now)
        )
        await db.commit()


async def set_phone(user_id: int, phone: str):
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET phone=?, phone_added_at=? WHERE user_id=?",
            (phone, now, user_id)
        )
        await db.commit()


async def get_phone(user_id: int) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT phone FROM users WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        return row[0] if row and row[0] else None


async def get_pro_until(user_id: int) -> datetime | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT pro_until FROM pro WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        if not row or not row[0]:
            return None
        return datetime.fromisoformat(row[0])


async def is_pro(user_id: int) -> bool:
    until = await get_pro_until(user_id)
    if not until:
        return False
    return until > datetime.now(timezone.utc)


async def grant_pro(user_id: int, days: int = PRO_DAYS_DEFAULT):
    until = datetime.now(timezone.utc) + timedelta(days=days)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO pro(user_id, pro_until)
            VALUES(?,?)
            ON CONFLICT(user_id) DO UPDATE SET pro_until=excluded.pro_until
            """,
            (user_id, until.isoformat())
        )
        await db.commit()


async def inc_usage(user_id: int) -> int:
    day = datetime.now(timezone.utc).date().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT cnt FROM usage WHERE user_id=? AND day=?", (user_id, day))
        row = await cur.fetchone()
        if row:
            cnt = row[0] + 1
            await db.execute("UPDATE usage SET cnt=? WHERE user_id=? AND day=?", (cnt, user_id, day))
        else:
            cnt = 1
            await db.execute("INSERT INTO usage(user_id, day, cnt) VALUES(?,?,?)", (user_id, day, cnt))
        await db.commit()
        return cnt


async def get_usage(user_id: int) -> int:
    day = datetime.now(timezone.utc).date().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT cnt FROM usage WHERE user_id=? AND day=?", (user_id, day))
        row = await cur.fetchone()
        return int(row[0]) if row else 0


async def stats_all():
    async with aiosqlite.connect(DB_PATH) as db:
        cur1 = await db.execute("SELECT COUNT(*) FROM users")
        total = (await cur1.fetchone())[0]
        cur2 = await db.execute("SELECT COUNT(*) FROM users WHERE phone IS NOT NULL AND phone != ''")
        phone = (await cur2.fetchone())[0]
        cur3 = await db.execute("SELECT COUNT(*) FROM pro WHERE pro_until > ?", (datetime.now(timezone.utc).isoformat(),))
        pro = (await cur3.fetchone())[0]
    return total, phone, pro


async def list_users(limit=50):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT user_id, username, full_name, phone FROM users ORDER BY joined_at DESC LIMIT ?",
            (limit,)
        )
        rows = await cur.fetchall()
    return rows


async def create_pro_request(user_id: int):
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO pro_requests(user_id, created_at, status) VALUES(?,?,?)",
            (user_id, now, "pending")
        )
        await db.commit()


async def set_pro_request_status(user_id: int, status: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE pro_requests SET status=? WHERE user_id=? AND status='pending'",
            (status, user_id)
        )
        await db.commit()


# -------------------- AI --------------------
async def ask_ai(user_text: str, mode: str) -> str:
    if not groq_client:
        return "AI tizimi sozlanmagan. GROQ_API_KEY ni tekshiring."

    system = (
        "Sen SmartYordam botisan. Javoblar Uzbek tilida bo‘lsin. "
        "Qisqa, aniq, tushunarli. Matematikada bosqichma-bosqich tushuntir. "
        "Ingliz tili bo‘lsa: grammar + misollar. Essay bo‘lsa: strukturali. "
        "Konspekt bo‘lsa: punktlar bilan."
    )

    if mode == "math":
        system += " Faqat foydali va to‘g‘ri hisob-kitob."
    elif mode == "english":
        system += " CEFR uslubida tushuntir."
    elif mode == "essay":
        system += " Kirish-Asosiy qism-Xulosa formatida."
    elif mode == "konspekt":
        system += " 1 betga sig‘adigan konspektga o‘xshat."

    try:
        resp = await groq_client.chat.completions.create(
            model="llama-3.1-70b-versatile",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_text},
            ],
            temperature=0.4,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        log.exception("AI error")
        return f"AI xatolik: {e}"


# -------------------- STATE (simple) --------------------
# user_id -> current_mode
USER_MODE: dict[int, str] = {}
# admin broadcast mode
ADMIN_WAIT_BROADCAST: set[int] = set()


# -------------------- ROUTER --------------------
router = Router()


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def ensure_phone_or_block(message: Message) -> bool:
    """Telefon bo‘lmasa: so‘raydi va bloklaydi."""
    uid = message.from_user.id
    phone = await get_phone(uid)
    if phone:
        return True
    await message.answer(
        "❗ Botdan foydalanish uchun avval telefon raqamingizni yuboring 👇",
        reply_markup=kb_phone_request()
    )
    return False


@router.message(CommandStart())
async def start(message: Message):
    await upsert_user(message)

    # Telefon tekshirish
    if not await get_phone(message.from_user.id):
        text = (
            "Salom! Men SmartYordam 🤖\n"
            "Matematika, ingliz tili, essay va konspekt bo‘yicha yordam beraman.\n\n"
            "Boshlash uchun avval telefon raqamingizni yuboring 👇"
        )
        await message.answer(text, reply_markup=kb_phone_request())
        return

    await message.answer(
        "Salom! Men SmartYordam 🤖\nMenyudan tanla yoki savol yoz 👇",
        reply_markup=kb_main()
    )


@router.message(F.contact)
async def on_contact(message: Message):
    await upsert_user(message)

    contact = message.contact
    if not contact:
        return

    # faqat o‘z raqamini qabul qilamiz
    if contact.user_id and contact.user_id != message.from_user.id:
        await message.answer("❌ Iltimos, o‘zingizning raqamingizni yuboring.", reply_markup=kb_phone_request())
        return

    phone = contact.phone_number
    await set_phone(message.from_user.id, phone)

    await message.answer("✅ Rahmat! Telefon raqamingiz saqlandi.\nEndi menyudan tanlang yoki savol yozing 👇",
                         reply_markup=kb_main())


@router.message(F.text == "🆔 MyID")
async def my_id_btn(message: Message):
    await upsert_user(message)
    await message.answer(f"Sizning Telegram ID: {message.from_user.id}")


@router.message(Command("id"))
async def my_id_cmd(message: Message):
    await upsert_user(message)
    await message.answer(f"Sizning Telegram ID: {message.from_user.id}")


@router.message(F.text == "📊 Limit")
async def limit(message: Message):
    await upsert_user(message)
    if not await ensure_phone_or_block(message):
        return

    used = await get_usage(message.from_user.id)
    pro = await is_pro(message.from_user.id)
    if pro:
        until = await get_pro_until(message.from_user.id)
        await message.answer(f"⭐ Siz PRO siz.\nPRO tugash: {until.astimezone().strftime('%Y-%m-%d %H:%M')}")
    else:
        await message.answer(f"🧾 Bugungi limit: {used}/{FREE_DAILY_LIMIT}\n⭐ PRO olsangiz cheksiz bo‘ladi.")


@router.message(F.text == "📚 Matematika")
async def mode_math(message: Message):
    await upsert_user(message)
    if not await ensure_phone_or_block(message):
        return
    USER_MODE[message.from_user.id] = "math"
    await message.answer("✅ Rejim: Matematika\nEndi masalangizni yozing 👇")


@router.message(F.text == "🇬🇧 Ingliz tili")
async def mode_english(message: Message):
    await upsert_user(message)
    if not await ensure_phone_or_block(message):
        return
    USER_MODE[message.from_user.id] = "english"
    await message.answer("✅ Rejim: Ingliz tili\nSavolingizni yozing (grammar, speaking, translate...) 👇")


@router.message(F.text == "📝 Essay")
async def mode_essay(message: Message):
    await upsert_user(message)
    if not await ensure_phone_or_block(message):
        return
    USER_MODE[message.from_user.id] = "essay"
    await message.answer("✅ Rejim: Essay\nMavzuni yozing va talablarni ayting (so‘z soni, uslub) 👇")


@router.message(F.text == "📖 Konspekt")
async def mode_konspekt(message: Message):
    await upsert_user(message)
    if not await ensure_phone_or_block(message):
        return
    USER_MODE[message.from_user.id] = "konspekt"
    await message.answer("✅ Rejim: Konspekt\nMavzuni yozing, men 1 betga mos qilib punktlar bilan beraman 👇")


@router.message(F.text == "⭐ PRO")
async def pro_info(message: Message):
    await upsert_user(message)
    if not await ensure_phone_or_block(message):
        return

    if await is_pro(message.from_user.id):
        until = await get_pro_until(message.from_user.id)
        await message.answer(f"⭐ Sizda PRO aktiv.\nTugash vaqti: {until.astimezone().strftime('%Y-%m-%d %H:%M')}")
        return

    text = (
        f"⭐ PRO = cheksiz savol + tezroq javob.\n\n"
        f"Narx: {PRO_PRICE_UZS:,} so‘m / {PRO_DAYS_DEFAULT} kun\n\n"
        f"To‘lov uchun karta:\n`{PAY_CARD}`\nEgasi: {PAY_OWNER}\n\n"
        f"To‘lov qiling, keyin pastdagi ✅ To‘ladim tugmasini bosing.\n"
        f"Admin tasdiqlasa PRO yoqiladi."
    )
    await message.answer(text, reply_markup=ikb_pro_pay(), parse_mode="Markdown")


@router.callback_query(F.data == "pro_paid")
async def pro_paid(callback: CallbackQuery):
    await callback.answer()
    uid = callback.from_user.id
    if not await get_phone(uid):
        await callback.message.answer("Avval telefon raqamingizni yuboring 👇", reply_markup=kb_phone_request())
        return

    await create_pro_request(uid)
    await callback.message.answer(
        "✅ So‘rovingiz adminga yuborildi.\n"
        "⏳ Admin tasdiqlasa PRO yoqiladi.\n\n"
        "Agar xohlasangiz, to‘lov screenshotini ham yuboring (ixtiyoriy)."
    )

    # adminlarga xabar
    for admin_id in ADMIN_IDS:
        try:
            await callback.bot.send_message(
                admin_id,
                f"💳 PRO so‘rovi!\nUser: @{callback.from_user.username or '—'}\nID: {uid}\n"
                f"Narx: {PRO_PRICE_UZS:,} so‘m\n"
                f"PRO berish/ rad etish:",
                reply_markup=ikb_admin_approve(uid)
            )
        except Exception:
            pass


@router.callback_query(F.data.startswith("admin_grant_pro:"))
async def admin_grant_pro_cb(callback: CallbackQuery):
    await callback.answer()
    admin_id = callback.from_user.id
    if not is_admin(admin_id):
        await callback.message.answer("⛔ Siz admin emassiz.")
        return

    user_id = int(callback.data.split(":")[1])
    await grant_pro(user_id, PRO_DAYS_DEFAULT)
    await set_pro_request_status(user_id, "approved")

    await callback.message.edit_text(f"✅ PRO berildi. User ID: {user_id}")

    try:
        await callback.bot.send_message(user_id, f"⭐ Tabriklayman! Sizga PRO {PRO_DAYS_DEFAULT} kunga yoqildi ✅")
    except Exception:
        pass


@router.callback_query(F.data.startswith("admin_reject_pro:"))
async def admin_reject_pro_cb(callback: CallbackQuery):
    await callback.answer()
    admin_id = callback.from_user.id
    if not is_admin(admin_id):
        await callback.message.answer("⛔ Siz admin emassiz.")
        return

    user_id = int(callback.data.split(":")[1])
    await set_pro_request_status(user_id, "rejected")
    await callback.message.edit_text(f"❌ Rad etildi. User ID: {user_id}")
    try:
        await callback.bot.send_message(user_id, "❌ PRO so‘rovingiz rad etildi. To‘lovni tekshirib qayta urinib ko‘ring.")
    except Exception:
        pass


@router.callback_query(F.data == "back_main")
async def back_main(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer("Menyudan tanla yoki savol yoz 👇", reply_markup=kb_main())


@router.message(F.text == "📊 Admin")
async def admin_panel(message: Message):
    await upsert_user(message)
    if not await ensure_phone_or_block(message):
        return

    if not is_admin(message.from_user.id):
        await message.answer("⛔ Siz admin emassiz.")
        return

    await message.answer("📊 Admin panel:", reply_markup=ikb_admin())


@router.callback_query(F.data == "admin_stats")
async def admin_stats(callback: CallbackQuery):
    await callback.answer()
    if not is_admin(callback.from_user.id):
        await callback.message.answer("⛔ Siz admin emassiz.")
        return

    total, phone, pro = await stats_all()
    await callback.message.answer(
        f"📊 Statistika:\n"
        f"👥 Jami user: {total}\n"
        f"📞 Telefon bergan: {phone}\n"
        f"⭐ PRO aktiv: {pro}"
    )


@router.callback_query(F.data == "admin_users")
async def admin_users(callback: CallbackQuery):
    await callback.answer()
    if not is_admin(callback.from_user.id):
        await callback.message.answer("⛔ Siz admin emassiz.")
        return

    rows = await list_users(limit=30)
    if not rows:
        await callback.message.answer("User yo‘q.")
        return

    lines = ["📋 Oxirgi 30 user:"]
    for (uid, username, full_name, phone) in rows:
        lines.append(f"- {uid} | @{username or '—'} | {full_name or '—'} | {phone or '—'}")
    msg = "\n".join(lines)
    if len(msg) > 3800:
        msg = msg[:3800] + "\n..."
    await callback.message.answer(msg)


@router.callback_query(F.data == "admin_broadcast")
async def admin_broadcast(callback: CallbackQuery):
    await callback.answer()
    if not is_admin(callback.from_user.id):
        await callback.message.answer("⛔ Siz admin emassiz.")
        return

    ADMIN_WAIT_BROADCAST.add(callback.from_user.id)
    await callback.message.answer("📣 Broadcast rejimi: Endi yuboradigan xabaringiz barcha userlarga boradi.\nBekor qilish: /cancel")


@router.message(Command("cancel"))
async def cancel(message: Message):
    if message.from_user.id in ADMIN_WAIT_BROADCAST:
        ADMIN_WAIT_BROADCAST.discard(message.from_user.id)
        await message.answer("✅ Broadcast bekor qilindi.", reply_markup=kb_main())
    else:
        await message.answer("OK.", reply_markup=kb_main())


@router.message()
async def any_text(message: Message):
    await upsert_user(message)

    # Admin broadcast mode
    if is_admin(message.from_user.id) and (message.from_user.id in ADMIN_WAIT_BROADCAST):
        text = message.text or ""
        ADMIN_WAIT_BROADCAST.discard(message.from_user.id)

        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("SELECT user_id FROM users")
            ids = [r[0] for r in await cur.fetchall()]

        ok = 0
        fail = 0
        for uid in ids:
            try:
                await message.bot.send_message(uid, f"📢 {text}")
                ok += 1
                await asyncio.sleep(0.05)
            except Exception:
                fail += 1

        await message.answer(f"✅ Broadcast tugadi.\nYuborildi: {ok}\nXato: {fail}", reply_markup=kb_main())
        return

    # Telefon majburiy
    if not await ensure_phone_or_block(message):
        return

    uid = message.from_user.id
    pro = await is_pro(uid)

    # Limit check (free users)
    if not pro:
        used = await get_usage(uid)
        if used >= FREE_DAILY_LIMIT:
            await message.answer(
                "⛔ Bugungi limit tugadi.\n⭐ PRO olsangiz cheksiz bo‘ladi.",
                reply_markup=kb_main()
            )
            return
        await inc_usage(uid)

    text = (message.text or "").strip()
    if not text:
        await message.answer("Savol yozing 🙂", reply_markup=kb_main())
        return

    mode = USER_MODE.get(uid, "math")  # default
    ans = await ask_ai(text, mode)
    await message.answer(ans, reply_markup=kb_main())


# -------------------- MAIN --------------------
async def main():
    await db_init()
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    log.info("Bot running... (polling)")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Bot stopped.")
