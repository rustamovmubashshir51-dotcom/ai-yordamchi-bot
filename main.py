# main.py
# Ai yordamchi bot (Aiogram v3) + Groq (OpenAI-compatible) + SQLite + Admin panel + Phone gating + Optional OCR

import os
import re
import time
import json
import logging
import asyncio
from datetime import datetime
from typing import Optional, List, Tuple

import aiosqlite

from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
)

# .env support (LOCAL uchun)
from dotenv import load_dotenv
load_dotenv()

# ---------- Logging ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("bot")

# ---------- ENV ----------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()

# Model (ixtiyoriy)
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b").strip()
GROQ_MODEL_FALLBACK = os.getenv("GROQ_MODEL_FALLBACK", GROQ_MODEL).strip()

# Limits / PRO
FREE_DAILY_LIMIT = int(os.getenv("FREE_DAILY_LIMIT", "20").strip() or 20)
PRO_DAYS_DEFAULT = int(os.getenv("PRO_DAYS", "30").strip() or 30)

# Admin IDs: "123,456"
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "").strip()
ADMIN_IDS: List[int] = []
for x in re.split(r"[,\s]+", ADMIN_IDS_RAW):
    if x.strip().isdigit():
        ADMIN_IDS.append(int(x.strip()))

# OCR (ixtiyoriy)
ENABLE_OCR = (os.getenv("ENABLE_OCR", "0").strip() == "1")
TESSERACT_CMD = os.getenv("TESSERACT_CMD", "").strip()

# Payments info (manual)
PAYMENT_CARD_TEXT = os.getenv("PAYMENT_CARD_TEXT", "To‘lov: 10 000 so‘m / oy (demo).").strip()
PAYMENT_INSTRUCTIONS = os.getenv(
    "PAYMENT_INSTRUCTIONS",
    "✅ PRO olish uchun admin bilan bog‘laning yoki to‘lov qiling.\n"
    "To‘lov qilgandan so‘ng skrinshot yuboring — admin PRO beradi."
).strip()

# DB file
DB_PATH = os.getenv("DB_PATH", "bot.db").strip() or "bot.db"

# ---------- Safety checks ----------
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN yo‘q. .env yoki Railway Variables ga BOT_TOKEN qo‘ying.")
if not GROQ_API_KEY:
    raise RuntimeError("GROQ_API_KEY yo‘q. .env yoki Railway Variables ga GROQ_API_KEY qo‘ying.")
if not ADMIN_IDS:
    log.warning("ADMIN_IDS bo‘sh. Admin panel ishlashi uchun ADMIN_IDS ga o‘zingizni Telegram ID yozing.")

# ---------- Optional OCR setup ----------
# OCR ishlatmoqchi bo'lsangiz: pip install pytesseract pillow
# Windows'da Tesseract o'rnating va TESSERACT_CMD ni .env ga bering.
pytesseract = None
Image = None
if ENABLE_OCR:
    try:
        import pytesseract as _pytesseract
        from PIL import Image as _Image
        pytesseract = _pytesseract
        Image = _Image
        if TESSERACT_CMD:
            pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD
        log.info("OCR enabled ✅")
    except Exception as e:
        log.error(f"OCR yoqilgan, lekin kutubxonalar topilmadi: {e}")
        ENABLE_OCR = False


# ---------- Helpers ----------
def now_ts() -> int:
    return int(time.time())

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def fmt_dt(ts: int) -> str:
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(ts)

def safe_int(x: str, default: int = 0) -> int:
    try:
        return int(x)
    except Exception:
        return default


# ---------- DB ----------
# users:
# user_id INTEGER PRIMARY KEY
# username TEXT
# first_name TEXT
# created_ts INTEGER
# last_seen_ts INTEGER
# phone TEXT
# phone_verified INTEGER (0/1)
# pro_until_ts INTEGER
#
# usage:
# user_id INTEGER
# day TEXT (YYYY-MM-DD)
# cnt INTEGER
# PRIMARY KEY(user_id, day)
#
# events:
# name TEXT PRIMARY KEY
# value INTEGER

db: Optional[aiosqlite.Connection] = None

async def db_init():
    global db
    db = await aiosqlite.connect(DB_PATH)
    await db.execute("""
    CREATE TABLE IF NOT EXISTS users(
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        created_ts INTEGER,
        last_seen_ts INTEGER,
        phone TEXT,
        phone_verified INTEGER DEFAULT 0,
        pro_until_ts INTEGER DEFAULT 0
    )""")
    await db.execute("""
    CREATE TABLE IF NOT EXISTS usage(
        user_id INTEGER,
        day TEXT,
        cnt INTEGER,
        PRIMARY KEY(user_id, day)
    )""")
    await db.execute("""
    CREATE TABLE IF NOT EXISTS events(
        name TEXT PRIMARY KEY,
        value INTEGER
    )""")
    await db.commit()

async def event_inc(name: str, by: int = 1):
    assert db is not None
    cur = await db.execute("SELECT value FROM events WHERE name=?", (name,))
    row = await cur.fetchone()
    if row is None:
        await db.execute("INSERT INTO events(name,value) VALUES(?,?)", (name, by))
    else:
        await db.execute("UPDATE events SET value=value+? WHERE name=?", (by, name))
    await db.commit()

async def event_get(name: str) -> int:
    assert db is not None
    cur = await db.execute("SELECT value FROM events WHERE name=?", (name,))
    row = await cur.fetchone()
    return int(row[0]) if row else 0

async def upsert_user(m: Message):
    assert db is not None
    uid = m.from_user.id
    username = (m.from_user.username or "").strip()
    first_name = (m.from_user.first_name or "").strip()
    ts = now_ts()

    cur = await db.execute("SELECT user_id FROM users WHERE user_id=?", (uid,))
    row = await cur.fetchone()
    if row is None:
        await db.execute(
            "INSERT INTO users(user_id, username, first_name, created_ts, last_seen_ts) VALUES(?,?,?,?,?)",
            (uid, username, first_name, ts, ts)
        )
    else:
        await db.execute(
            "UPDATE users SET username=?, first_name=?, last_seen_ts=? WHERE user_id=?",
            (username, first_name, ts, uid)
        )
    await db.commit()

async def set_phone(user_id: int, phone: str, verified: bool = True):
    assert db is not None
    await db.execute(
        "UPDATE users SET phone=?, phone_verified=? WHERE user_id=?",
        (phone, 1 if verified else 0, user_id)
    )
    await db.commit()

async def get_phone(user_id: int) -> Tuple[Optional[str], bool]:
    assert db is not None
    cur = await db.execute("SELECT phone, phone_verified FROM users WHERE user_id=?", (user_id,))
    row = await cur.fetchone()
    if not row:
        return None, False
    phone = row[0]
    verified = bool(row[1] or 0)
    return phone, verified

async def get_pro_until(user_id: int) -> int:
    assert db is not None
    cur = await db.execute("SELECT pro_until_ts FROM users WHERE user_id=?", (user_id,))
    row = await cur.fetchone()
    return int(row[0] or 0) if row else 0

async def set_pro_until(user_id: int, ts: int):
    assert db is not None
    await db.execute("UPDATE users SET pro_until_ts=? WHERE user_id=?", (ts, user_id))
    await db.commit()

async def is_pro(user_id: int) -> bool:
    return now_ts() < await get_pro_until(user_id)

async def usage_day_key() -> str:
    return datetime.now().strftime("%Y-%m-%d")

async def usage_get(user_id: int) -> int:
    assert db is not None
    day = await usage_day_key()
    cur = await db.execute("SELECT cnt FROM usage WHERE user_id=? AND day=?", (user_id, day))
    row = await cur.fetchone()
    return int(row[0]) if row else 0

async def usage_inc(user_id: int, by: int = 1):
    assert db is not None
    day = await usage_day_key()
    cur = await db.execute("SELECT cnt FROM usage WHERE user_id=? AND day=?", (user_id, day))
    row = await cur.fetchone()
    if row is None:
        await db.execute("INSERT INTO usage(user_id, day, cnt) VALUES(?,?,?)", (user_id, day, by))
    else:
        await db.execute("UPDATE usage SET cnt=cnt+? WHERE user_id=? AND day=?", (by, user_id, day))
    await db.commit()

async def users_count() -> int:
    assert db is not None
    cur = await db.execute("SELECT COUNT(*) FROM users")
    row = await cur.fetchone()
    return int(row[0]) if row else 0

async def users_started_count() -> int:
    # start event orqali (start bosishlar soni)
    return await event_get("start_total")

async def users_phone_count() -> int:
    assert db is not None
    cur = await db.execute("SELECT COUNT(*) FROM users WHERE phone_verified=1")
    row = await cur.fetchone()
    return int(row[0]) if row else 0


# ---------- Groq OpenAI-compatible call ----------
# Groq OpenAI compatible endpoint: https://api.groq.com/openai/v1/chat/completions
# requests emas, aiohttp ishlatamiz.
import aiohttp

SYSTEM_PROMPT = (
    "Siz professional o‘qituvchi yordamchisiz. Foydalanuvchining savoliga aniq, tushunarli, "
    "bosqichma-bosqich va qisqa misollar bilan javob bering. Til: o‘zbek. "
    "Matematika bo‘lsa: formulalar va hisob-kitobni ko‘rsating. "
    "Ingliz tili bo‘lsa: grammatikani oddiy tushuntiring."
)

async def groq_chat(user_text: str, mode: str = "general") -> str:
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }

    # mode bo'yicha kichik yo'naltirish
    if mode == "math":
        sys = SYSTEM_PROMPT + " Siz matematikaga fokus qilasiz."
    elif mode == "english":
        sys = SYSTEM_PROMPT + " Siz ingliz tili o‘qituvchisiz."
    elif mode == "essay":
        sys = SYSTEM_PROMPT + " Siz essay yozish bo‘yicha yordamchisiz."
    elif mode == "summary":
        sys = SYSTEM_PROMPT + " Siz konspekt va qisqa xulosa bo‘yicha yordamchisiz."
    else:
        sys = SYSTEM_PROMPT

    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": sys},
            {"role": "user", "content": user_text},
        ],
        "temperature": 0.4,
    }

    url = "https://api.groq.com/openai/v1/chat/completions"

    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(url, headers=headers, data=json.dumps(payload), timeout=60) as resp:
                if resp.status != 200:
                    txt = await resp.text()
                    raise RuntimeError(f"Groq error {resp.status}: {txt[:2000]}")
                data = await resp.json()
                return (data["choices"][0]["message"]["content"] or "").strip()
        except Exception as e:
            # fallback model
            if GROQ_MODEL_FALLBACK and GROQ_MODEL_FALLBACK != GROQ_MODEL:
                payload["model"] = GROQ_MODEL_FALLBACK
                async with session.post(url, headers=headers, data=json.dumps(payload), timeout=60) as resp2:
                    if resp2.status != 200:
                        txt2 = await resp2.text()
                        raise RuntimeError(f"Groq fallback error {resp2.status}: {txt2[:2000]}")
                    data2 = await resp2.json()
                    return (data2["choices"][0]["message"]["content"] or "").strip()
            raise e


# ---------- UI ----------
def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📞 Telefon yuborish", request_contact=True)],
            [KeyboardButton(text="📚 Matematika"), KeyboardButton(text="🇬🇧 Ingliz tili")],
            [KeyboardButton(text="📝 Essay"), KeyboardButton(text="🧾 Konspekt")],
            [KeyboardButton(text="⭐ PRO"), KeyboardButton(text="📊 Admin")],
        ],
        resize_keyboard=True
    )

def pro_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 PRO olish (10 000 so‘m)", callback_data="buy_pro")],
            [InlineKeyboardButton(text="🎁 30 kun demo (admin beradi)", callback_data="demo_pro")],
        ]
    )


# ---------- Bot / Router ----------
router = Router()


# ---------- Guards ----------
async def ensure_phone(message: Message) -> bool:
    """Telefon verifikatsiya bo'lmasa, to'xtatadi."""
    uid = message.from_user.id
    phone, verified = await get_phone(uid)
    if verified:
        return True
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📞 Telefon raqamni yuborish", request_contact=True)]],
        resize_keyboard=True
    )
    await message.answer("❗ Botdan foydalanish uchun telefon raqamingizni yuboring 👇", reply_markup=kb)
    return False

async def ensure_limit(message: Message) -> bool:
    uid = message.from_user.id
    if await is_pro(uid):
        return True
    used = await usage_get(uid)
    if used >= FREE_DAILY_LIMIT:
        await message.answer(
            f"⛔ Bugungi limit tugadi: {used}/{FREE_DAILY_LIMIT}\n\n"
            f"⭐ PRO olsangiz cheksiz savol berasiz.",
            reply_markup=pro_kb()
        )
        return False
    return True


# ---------- /start ----------
@router.message(CommandStart())
async def cmd_start(message: Message):
    await upsert_user(message)
    await event_inc("start_total", 1)

    await message.answer(
        "Salom! Men SmartYordam 🤖\n"
        "Matematika, ingliz tili, essay va konspekt bo‘yicha yordam beraman.\n\n"
        "Boshlash uchun avval telefon raqamingizni yuboring 👇",
        reply_markup=main_menu()
    )
    # Telefon so'rash
    await ensure_phone(message)


# ---------- Contact handler ----------
@router.message(F.contact)
async def on_contact(message: Message):
    await upsert_user(message)
    uid = message.from_user.id

    # Faqat o'z kontaktingni qabul qilamiz
    if message.contact.user_id and message.contact.user_id != uid:
        await message.answer("❗ Iltimos, o‘zingizning telefon raqamingizni yuboring.", reply_markup=main_menu())
        return

    phone = (message.contact.phone_number or "").strip()
    await set_phone(uid, phone, verified=True)
    await event_inc("phone_total", 1)

    await message.answer("✅ Rahmat! Telefon raqamingiz saqlandi.\nEndi menyudan tanlang yoki savol yozing 👇", reply_markup=main_menu())


# ---------- Mode selection ----------
USER_MODE = {}  # uid -> mode (memory, soddalashtirilgan). Railway restartda tozalanadi, ammo ok.

def set_mode(uid: int, mode: str):
    USER_MODE[uid] = mode

def get_mode(uid: int) -> str:
    return USER_MODE.get(uid, "general")


@router.message(F.text == "📚 Matematika")
async def mode_math(message: Message):
    await upsert_user(message)
    if not await ensure_phone(message):
        return
    set_mode(message.from_user.id, "math")
    await message.answer("✅ Rejim: math\nEndi savolingizni yuboring.", reply_markup=main_menu())

@router.message(F.text == "🇬🇧 Ingliz tili")
async def mode_english(message: Message):
    await upsert_user(message)
    if not await ensure_phone(message):
        return
    set_mode(message.from_user.id, "english")
    await message.answer("✅ Rejim: english\nEndi savolingizni yuboring.", reply_markup=main_menu())

@router.message(F.text == "📝 Essay")
async def mode_essay(message: Message):
    await upsert_user(message)
    if not await ensure_phone(message):
        return
    set_mode(message.from_user.id, "essay")
    await message.answer("✅ Rejim: essay\nMavzu/Topshiriqni yuboring.", reply_markup=main_menu())

@router.message(F.text == "🧾 Konspekt")
async def mode_summary(message: Message):
    await upsert_user(message)
    if not await ensure_phone(message):
        return
    set_mode(message.from_user.id, "summary")
    await message.answer("✅ Rejim: konspekt\nMatnni yuboring, men qisqartirib beraman.", reply_markup=main_menu())


# ---------- PRO ----------
@router.message(F.text == "⭐ PRO")
async def pro_info(message: Message):
    await upsert_user(message)
    if not await ensure_phone(message):
        return

    uid = message.from_user.id
    if await is_pro(uid):
        until = await get_pro_until(uid)
        await message.answer(f"⭐ Sizda PRO aktiv ✅\n⏳ Tugash vaqti: {fmt_dt(until)}", reply_markup=main_menu())
        return

    await message.answer(
        "⭐ PRO = cheksiz savol + tezroq javob.\n\n"
        f"Narx: 10 000 so‘m / oy.\n\n"
        f"{PAYMENT_CARD_TEXT}\n\n"
        "Pastdagi tugmani bosing:",
        reply_markup=pro_kb()
    )

@router.callback_query(F.data == "buy_pro")
async def cb_buy_pro(call: CallbackQuery):
    await call.answer()
    uid = call.from_user.id
    msg = (
        "💳 PRO sotib olish\n\n"
        f"{PAYMENT_INSTRUCTIONS}\n\n"
        "✅ To‘lov qildingizmi? Skrinshot yuboring. Admin tasdiqlaydi."
    )
    await call.message.answer(msg, reply_markup=main_menu())

@router.callback_query(F.data == "demo_pro")
async def cb_demo_pro(call: CallbackQuery):
    await call.answer()
    await call.message.answer(
        "🎁 Demo PRO faqat admin tomonidan beriladi.\n"
        "Adminga yozing yoki skrinshot yuboring.",
        reply_markup=main_menu()
    )


# ---------- Admin panel ----------
@router.message(F.text == "📊 Admin")
async def admin_panel(message: Message):
    await upsert_user(message)
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Siz admin emassiz.", reply_markup=main_menu())
        return

    total_users = await users_count()
    start_total = await users_started_count()
    phone_total = await users_phone_count()

    await message.answer(
        "📊 Admin panel\n\n"
        f"👥 Barcha userlar: {total_users}\n"
        f"▶️ /start bosishlar: {start_total}\n"
        f"📞 Telefon berganlar: {phone_total}\n\n"
        "Buyruqlar:\n"
        "• /stats — batafsil statistika\n"
        "• /pro <user_id> <days> — PRO berish\n"
        "• /unpro <user_id> — PRO olib tashlash\n"
        "• /who <user_id> — user info\n",
        reply_markup=main_menu()
    )

@router.message(Command("stats"))
async def cmd_stats(message: Message):
    await upsert_user(message)
    if not is_admin(message.from_user.id):
        return
    total_users = await users_count()
    start_total = await users_started_count()
    phone_total = await users_phone_count()
    await message.answer(
        "📊 Stats\n\n"
        f"Users: {total_users}\n"
        f"Start events: {start_total}\n"
        f"Phone verified: {phone_total}\n"
    )

@router.message(Command("pro"))
async def cmd_pro(message: Message):
    await upsert_user(message)
    if not is_admin(message.from_user.id):
        return

    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("❗ Format: /pro <user_id> <days>\nMasalan: /pro 123456789 30")
        return

    user_id = safe_int(parts[1], 0)
    days = safe_int(parts[2], PRO_DAYS_DEFAULT) if len(parts) >= 3 else PRO_DAYS_DEFAULT
    if user_id <= 0:
        await message.answer("❗ user_id noto‘g‘ri.")
        return

    until = now_ts() + days * 24 * 3600
    await set_pro_until(user_id, until)
    await message.answer(f"✅ PRO berildi: user={user_id}, days={days}, until={fmt_dt(until)}")

@router.message(Command("unpro"))
async def cmd_unpro(message: Message):
    await upsert_user(message)
    if not is_admin(message.from_user.id):
        return

    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("❗ Format: /unpro <user_id>")
        return
    user_id = safe_int(parts[1], 0)
    if user_id <= 0:
        await message.answer("❗ user_id noto‘g‘ri.")
        return
    await set_pro_until(user_id, 0)
    await message.answer(f"✅ PRO o‘chirildi: user={user_id}")

@router.message(Command("who"))
async def cmd_who(message: Message):
    await upsert_user(message)
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("❗ Format: /who <user_id>")
        return
    user_id = safe_int(parts[1], 0)
    assert db is not None
    cur = await db.execute(
        "SELECT user_id, username, first_name, created_ts, last_seen_ts, phone, phone_verified, pro_until_ts FROM users WHERE user_id=?",
        (user_id,)
    )
    row = await cur.fetchone()
    if not row:
        await message.answer("Topilmadi.")
        return
    (uid, username, first_name, created_ts, last_seen_ts, phone, phone_verified, pro_until_ts) = row
    await message.answer(
        f"👤 User info\n\n"
        f"ID: {uid}\n"
        f"Username: @{username}\n"
        f"Name: {first_name}\n"
        f"Created: {fmt_dt(created_ts)}\n"
        f"Last seen: {fmt_dt(last_seen_ts)}\n"
        f"Phone: {phone}\n"
        f"Phone verified: {bool(phone_verified)}\n"
        f"PRO until: {fmt_dt(int(pro_until_ts or 0))}\n"
    )


# ---------- Photo -> OCR -> Answer ----------
@router.message(F.photo)
async def photo_handler(message: Message):
    await upsert_user(message)
    if not await ensure_phone(message):
        return

    if not ENABLE_OCR:
        await message.answer(
            "📷 Rasm qabul qilindi, lekin OCR yoqilmagan.\n"
            "Admin: ENABLE_OCR=1 qilib, pytesseract + pillow o‘rnating.",
            reply_markup=main_menu()
        )
        return

    if not await ensure_limit(message):
        return

    try:
        # eng katta rasmni olamiz
        file_id = message.photo[-1].file_id
        bot: Bot = message.bot
        file = await bot.get_file(file_id)

        # yuklab olish
        file_bytes = await bot.download_file(file.file_path)

        # PIL o'qish
        img = Image.open(file_bytes)
        text = pytesseract.image_to_string(img, lang="eng+rus")  # uzb tesseract bo'lmasa ham bo'ladi
        text = (text or "").strip()

        if not text:
            await message.answer("❗ Rasmdan matn topilmadi. Yaxshiroq rasm yuboring.", reply_markup=main_menu())
            return

        await usage_inc(message.from_user.id, 1)
        mode = get_mode(message.from_user.id)
        prompt = f"Quyidagi matndan masalani/topshiriqni tushunib yechib ber:\n\n{text}"
        ans = await groq_chat(prompt, mode=mode)

        await message.answer(ans[:3900], reply_markup=main_menu())
    except Exception as e:
        log.exception("photo_handler error")
        await message.answer(f"❌ OCR xatolik: {e}", reply_markup=main_menu())


# ---------- Text handler (main) ----------
@router.message(F.text)
async def text_handler(message: Message):
    await upsert_user(message)

    # admin commands already handled by Command filters
    text = (message.text or "").strip()

    # Menyudagi tugmalar / start / etc bo'lsa, bu yerda chiqib ketadi
    if text in {"📚 Matematika", "🇬🇧 Ingliz tili", "📝 Essay", "🧾 Konspekt", "⭐ PRO", "📊 Admin"}:
        return

    if not await ensure_phone(message):
        return

    if not await ensure_limit(message):
        return

    uid = message.from_user.id
    mode = get_mode(uid)

    try:
        await usage_inc(uid, 1)
        ans = await groq_chat(text, mode=mode)
        if not ans:
            ans = "❗ Javob topilmadi. Savolni boshqacha yozib ko‘ring."
        await message.answer(ans[:3900], reply_markup=main_menu())
    except Exception as e:
        log.exception("text_handler error")
        await message.answer(f"❌ Xatolik: {e}")


# ---------- App ----------
async def main():
    await db_init()

    bot = Bot(BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    log.info("Bot running ✅")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
