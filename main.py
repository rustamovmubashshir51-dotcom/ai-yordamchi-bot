import os
import re
import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Optional, List, Tuple

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext

import aiosqlite
from openai import OpenAI

# -----------------------
# CONFIG
# -----------------------
load_dotenv()
TZ = ZoneInfo("Asia/Tashkent")

def env_int(name: str, default: int) -> int:
    v = os.getenv(name, str(default)).strip()
    try:
        return int(v)
    except:
        return default

def env_str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()

@dataclass(frozen=True)
class Settings:
    bot_token: str
    groq_key: str
    model: str
    model_fallback: str
    free_daily_limit: int
    pro_days: int
    admin_ids: List[int]
    ref_bonus_days: int
    ref_need: int
    enable_ocr: bool
    tesseract_cmd: str
    db_path: str

def parse_admin_ids(s: str) -> List[int]:
    out = []
    for p in s.split(","):
        p = p.strip()
        if p.isdigit():
            out.append(int(p))
    return out

S = Settings(
    bot_token=env_str("BOT_TOKEN"),
    groq_key=env_str("GROQ_API_KEY"),
    model=env_str("GROQ_MODEL", "openai/gpt-oss-20b"),
    model_fallback=env_str("GROQ_MODEL_FALLBACK", "openai/gpt-oss-20b"),
    free_daily_limit=env_int("FREE_DAILY_LIMIT", 5),
    pro_days=env_int("PRO_DAYS", 30),
    admin_ids=parse_admin_ids(env_str("ADMIN_IDS", "")),
    ref_bonus_days=env_int("REF_BONUS_DAYS", 1),
    ref_need=env_int("REF_NEED", 3),
    enable_ocr=env_str("ENABLE_OCR", "1") == "1",
    tesseract_cmd=env_str("TESSERACT_CMD", ""),
    db_path=env_str("DB_PATH", "bot.db"),
)

if not S.bot_token:
    raise RuntimeError("BOT_TOKEN yo‘q (.env).")
if not S.groq_key:
    raise RuntimeError("GROQ_API_KEY yo‘q (.env).")

# -----------------------
# LOGGING
# -----------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("ai_yordamchi")

# -----------------------
# DB
# -----------------------
def now_ts() -> int:
    return int(datetime.now(TZ).timestamp())

def today_key() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")

async def init_db():
    async with aiosqlite.connect(S.db_path) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY,
            created_ts INTEGER NOT NULL,
            pro_until_ts INTEGER NOT NULL DEFAULT 0,
            ref_code TEXT,
            ref_by INTEGER,
            ref_count INTEGER NOT NULL DEFAULT 0
        )""")
        await db.execute("""
        CREATE TABLE IF NOT EXISTS usage(
            user_id INTEGER NOT NULL,
            day TEXT NOT NULL,
            used INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(user_id, day)
        )""")
        await db.commit()

def make_ref_code(user_id: int) -> str:
    # short referral code
    return f"ref{user_id}"

async def ensure_user(user_id: int, ref_by: Optional[int] = None):
    async with aiosqlite.connect(S.db_path) as db:
        cur = await db.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        if row:
            return

        await db.execute("""
        INSERT INTO users(user_id, created_ts, pro_until_ts, ref_code, ref_by, ref_count)
        VALUES (?, ?, 0, ?, ?, 0)
        """, (user_id, now_ts(), make_ref_code(user_id), ref_by, ))
        await db.commit()

async def get_pro_until(user_id: int) -> int:
    async with aiosqlite.connect(S.db_path) as db:
        cur = await db.execute("SELECT pro_until_ts FROM users WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        return int(row[0]) if row else 0

async def is_pro(user_id: int) -> bool:
    return now_ts() < await get_pro_until(user_id)

async def add_pro_days(user_id: int, days: int):
    cur_until = await get_pro_until(user_id)
    base = max(cur_until, now_ts())
    new_until = base + int(timedelta(days=days).total_seconds())
    async with aiosqlite.connect(S.db_path) as db:
        await db.execute("UPDATE users SET pro_until_ts=? WHERE user_id=?", (new_until, user_id))
        await db.commit()

async def get_used_today(user_id: int) -> int:
    day = today_key()
    async with aiosqlite.connect(S.db_path) as db:
        cur = await db.execute("SELECT used FROM usage WHERE user_id=? AND day=?", (user_id, day))
        row = await cur.fetchone()
        return int(row[0]) if row else 0

async def inc_used_today(user_id: int, n: int = 1):
    day = today_key()
    async with aiosqlite.connect(S.db_path) as db:
        await db.execute("""
        INSERT INTO usage(user_id, day, used) VALUES(?, ?, ?)
        ON CONFLICT(user_id, day) DO UPDATE SET used = used + ?
        """, (user_id, day, n, n))
        await db.commit()

async def get_ref_info(user_id: int) -> Tuple[str, int]:
    async with aiosqlite.connect(S.db_path) as db:
        cur = await db.execute("SELECT ref_code, ref_count FROM users WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        if not row:
            return (make_ref_code(user_id), 0)
        return (row[0] or make_ref_code(user_id), int(row[1]))

async def apply_referral_if_new(new_user_id: int, ref_code: str):
    # find owner by ref_code
    async with aiosqlite.connect(S.db_path) as db:
        cur = await db.execute("SELECT user_id FROM users WHERE ref_code=?", (ref_code,))
        row = await cur.fetchone()
        if not row:
            return False, "Ref code topilmadi."
        owner_id = int(row[0])
        if owner_id == new_user_id:
            return False, "O'zingizni ref qila olmaysiz."

        # check if already has ref_by
        cur2 = await db.execute("SELECT ref_by FROM users WHERE user_id=?", (new_user_id,))
        row2 = await cur2.fetchone()
        if row2 and row2[0]:
            return False, "Sizda ref allaqachon bor."

        # set ref_by and increment owner's ref_count
        await db.execute("UPDATE users SET ref_by=? WHERE user_id=?", (owner_id, new_user_id))
        await db.execute("UPDATE users SET ref_count = ref_count + 1 WHERE user_id=?", (owner_id,))
        await db.commit()

        # check if owner reached target -> give bonus days
        cur3 = await db.execute("SELECT ref_count FROM users WHERE user_id=?", (owner_id,))
        row3 = await cur3.fetchone()
        cnt = int(row3[0]) if row3 else 0
        bonus_given = False
        if cnt > 0 and (cnt % S.ref_need) == 0:
            # give bonus to owner
            # do outside to reuse add_pro_days but inside db connection is ok too—keep simple:
            pass

    # bonus if hit milestone
    if cnt > 0 and (cnt % S.ref_need) == 0:
        await add_pro_days(owner_id, S.ref_bonus_days)
        bonus_given = True

    return True, f"Ref qabul qilindi. {'Bonus PRO berildi ✅' if bonus_given else ''}"

# -----------------------
# AI (Groq OpenAI-compatible)
# -----------------------
client = OpenAI(api_key=S.groq_key, base_url="https://api.groq.com/openai/v1")

SYSTEM = """
Sen Ai yordamchi-bot. Til: O'zbek.
Vazifa: student/o'quvchiga dars yordam.
Javoblar:
- qisqa natija (1-2 qator)
- keyin bosqichma-bosqich tushuntirish
- kerak bo‘lsa 2 ta misol
Hech qachon keraksiz uzun gapirma.
"""

def detect_mode(text: str) -> str:
    t = (text or "").lower()
    if re.search(r"\b(matema|hisob|tenglama|integral|foiz|kvadrat|logarifm|sin|cos|tan)\b", t):
        return "math"
    if re.search(r"\b(ingliz|grammar|translate|tarjima|ielts|present|past|future)\b", t):
        return "english"
    if re.search(r"\b(essay|insho|maqola)\b", t):
        return "essay"
    if re.search(r"\b(konspekt|xulosa|qisqartir)\b", t):
        return "summary"
    return "homework"

def ask_ai_sync(user_text: str, mode_hint: str) -> str:
    prompt = f"Rejim: {mode_hint}\nSavol:\n{user_text}"
    try:
        r = client.responses.create(
            model=S.model,
            input=[
                {"role": "system", "content": SYSTEM.strip()},
                {"role": "user", "content": prompt},
            ],
        )
        out = (r.output_text or "").strip()
        if out:
            return out
    except Exception as e:
        log.warning("Primary model failed: %s", e)

    # fallback
    r2 = client.responses.create(
        model=S.model_fallback,
        input=[
            {"role": "system", "content": SYSTEM.strip()},
            {"role": "user", "content": prompt},
        ],
    )
    return (r2.output_text or "").strip()

# -----------------------
# OCR (optional)
# -----------------------
OCR_READY = False
if S.enable_ocr:
    try:
        from PIL import Image
        import pytesseract
        from io import BytesIO
        if S.tesseract_cmd:
            pytesseract.pytesseract.tesseract_cmd = S.tesseract_cmd
        OCR_READY = True
    except Exception as e:
        OCR_READY = False
        log.warning("OCR not ready: %s", e)

# -----------------------
# FSM: mode saved
# -----------------------
class UserState(StatesGroup):
    mode = State()  # keeps selected mode

# -----------------------
# UI
# -----------------------
def menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📚 Matematika"), KeyboardButton(text="🇬🇧 Ingliz tili")],
            [KeyboardButton(text="📝 Essay"), KeyboardButton(text="📖 Konspekt")],
            [KeyboardButton(text="⭐ PRO"), KeyboardButton(text="🎁 Referral")],
            [KeyboardButton(text="ℹ️ Limit"), KeyboardButton(text="🆔 MyID")],
        ],
        resize_keyboard=True,
    )

def human_time(ts: int) -> str:
    if ts <= 0:
        return "yo‘q"
    dt = datetime.fromtimestamp(ts, TZ)
    return dt.strftime("%Y-%m-%d %H:%M")

# -----------------------
# BOT
# -----------------------
router = Router()

async def block_if_limit(m: Message) -> bool:
    """True => blocked"""
    if await is_pro(m.from_user.id):
        return False
    used = await get_used_today(m.from_user.id)
    if used >= S.free_daily_limit:
        await m.answer("⛔ Bugungi bepul limit tugadi.\n⭐ PRO olsangiz cheksiz bo‘ladi.")
        return True
    return False

async def send_long(m: Message, text: str):
    text = (text or "").strip()
    if not text:
        await m.answer("Javob chiqarmadi. Boshqacha yozib ko‘ring.")
        return
    for i in range(0, len(text), 3800):
        await m.answer(text[i:i+3800])

@router.message(CommandStart())
async def start(m: Message, state: FSMContext):
    # referral parse: /start ref123
    args = (m.text or "").split(maxsplit=1)
    ref_code = args[1].strip() if len(args) > 1 else ""

    await ensure_user(m.from_user.id)
    await state.set_state(UserState.mode)
    await state.update_data(mode="homework")

    if ref_code:
        ok, msg = await apply_referral_if_new(m.from_user.id, ref_code)
        if ok:
            await m.answer(f"✅ {msg}")

    await m.answer(
        "Salom! Men **Ai yordamchi-bot** 🤖\n"
        "Matematika, ingliz tili, essay va konspektda yordam beraman.\n"
        "📸 Rasm yuborsang ham yechib beraman (OCR yoqilgan bo‘lsa).\n\n"
        "Menyudan tanla yoki savol yoz 👇",
        reply_markup=menu(),
        parse_mode="Markdown",
    )

@router.message(Command("status"))
async def status(m: Message):
    await ensure_user(m.from_user.id)
    pro_until = await get_pro_until(m.from_user.id)
    pro = await is_pro(m.from_user.id)
    used = await get_used_today(m.from_user.id)
    await m.answer(
        f"⭐ PRO: {'ha' if pro else 'yo‘q'}\n"
        f"PRO tugash: {human_time(pro_until)}\n"
        f"Bugun ishlatilgan: {used}/{S.free_daily_limit}\n"
        f"OCR: {'yoqilgan' if OCR_READY else 'o‘chiq'}\n"
        f"Model: {S.model}"
    )

@router.message(F.text == "🆔 MyID")
async def myid(m: Message):
    await m.answer(f"Sizning ID: {m.from_user.id}")

@router.message(F.text == "ℹ️ Limit")
async def limit(m: Message):
    await ensure_user(m.from_user.id)
    if await is_pro(m.from_user.id):
        await m.answer("⭐ Siz PROsiz — cheksiz ✅")
        return
    used = await get_used_today(m.from_user.id)
    left = max(0, S.free_daily_limit - used)
    await m.answer(f"Bugungi limit: {S.free_daily_limit}\nIshlatilgan: {used}\nQolgan: {left}")

@router.message(F.text == "🎁 Referral")
async def referral(m: Message):
    await ensure_user(m.from_user.id)
    ref_code, cnt = await get_ref_info(m.from_user.id)
    link = f"https://t.me/{(await m.bot.get_me()).username}?start={ref_code}"
    await m.answer(
        f"🎁 Referral link:\n{link}\n\n"
        f"Takliflar: {cnt}\n"
        f"Har {S.ref_need} ta taklif uchun +{S.ref_bonus_days} kun PRO ✅"
    )

@router.message(F.text == "⭐ PRO")
async def pro(m: Message):
    await m.answer(
        "⭐ PRO = cheksiz savol + tezroq javob.\n\n"
        "Hozir demo:\n"
        "👉 /buy_pro — 30 kun PRO berish (demo)\n"
        "Keyin Click/Payme ulaymiz."
    )

@router.message(Command("buy_pro"))
async def buy_pro_demo(m: Message):
    await ensure_user(m.from_user.id)
    await add_pro_days(m.from_user.id, S.pro_days)
    await m.answer(f"✅ PRO berildi: +{S.pro_days} kun (demo).")

@router.message(Command("admin"))
async def admin_help(m: Message):
    if m.from_user.id not in S.admin_ids:
        await m.answer("⛔ Admin emassiz.")
        return
    await m.answer(
        "Admin komandalar:\n"
        "/givepro <user_id> <days>\n"
        "/stats"
    )

@router.message(Command("givepro"))
async def admin_givepro(m: Message):
    if m.from_user.id not in S.admin_ids:
        await m.answer("⛔ Admin emassiz.")
        return
    parts = (m.text or "").split()
    if len(parts) != 3 or (not parts[1].isdigit()) or (not parts[2].isdigit()):
        await m.answer("Format: /givepro 123456789 30")
        return
    uid = int(parts[1]); days = int(parts[2])
    await ensure_user(uid)
    await add_pro_days(uid, days)
    await m.answer(f"✅ {uid} ga +{days} kun PRO berildi.")

@router.message(Command("stats"))
async def admin_stats(m: Message):
    if m.from_user.id not in S.admin_ids:
        await m.answer("⛔ Admin emassiz.")
        return
    async with aiosqlite.connect(S.db_path) as db:
        cur1 = await db.execute("SELECT COUNT(*) FROM users")
        users = (await cur1.fetchone())[0]
        cur2 = await db.execute("SELECT COUNT(*) FROM users WHERE pro_until_ts > ?", (now_ts(),))
        pro_users = (await cur2.fetchone())[0]
    await m.answer(f"👥 Users: {users}\n⭐ PRO users: {pro_users}")

@router.message(F.text.in_({"📚 Matematika", "🇬🇧 Ingliz tili", "📝 Essay", "📖 Konspekt"}))
async def set_mode(m: Message, state: FSMContext):
    mapping = {
        "📚 Matematika": "math",
        "🇬🇧 Ingliz tili": "english",
        "📝 Essay": "essay",
        "📖 Konspekt": "summary",
    }
    mode = mapping.get(m.text, "homework")
    await state.set_state(UserState.mode)
    await state.update_data(mode=mode)
    await m.answer(f"✅ Rejim: {mode}\nEndi savol yubor.")

@router.message(F.photo)
async def photo_handler(m: Message, state: FSMContext):
    await ensure_user(m.from_user.id)
    if await block_if_limit(m):
        return

    if not OCR_READY:
        await m.answer("📸 Rasm qabul qilindi, lekin OCR tayyor emas.\n`pip install pillow pytesseract` + Tesseract o‘rnatish kerak.")
        return

    await m.chat.do("typing")
    try:
        from io import BytesIO
        photo = m.photo[-1]
        file = await m.bot.get_file(photo.file_id)
        fbytes = await m.bot.download_file(file.file_path)

        from PIL import Image
        import pytesseract

        img = Image.open(BytesIO(fbytes.read()))
        extracted = (pytesseract.image_to_string(img) or "").strip()

        if not extracted:
            await m.answer("Rasm ichidan matn topilmadi. Tiniqroq rasm yubor.")
            return

        data = await state.get_data()
        mode = data.get("mode", "homework")

        user_text = f"Rasmdagi matn:\n{extracted}\n\nShuni yech/tushuntir."
        answer = await asyncio.to_thread(ask_ai_sync, user_text, mode)

        if not await is_pro(m.from_user.id):
            await inc_used_today(m.from_user.id, 1)

        await send_long(m, answer)
    except Exception as e:
        log.exception("photo error")
        await m.answer(f"⚠️ Xatolik: {type(e).__name__}\nTerminaldagi errorni copy qilib yubor.")

@router.message(F.text)
async def text_handler(m: Message, state: FSMContext):
    await ensure_user(m.from_user.id)
    if await block_if_limit(m):
        return

    data = await state.get_data()
    mode = data.get("mode") or detect_mode(m.text)

    await m.chat.do("typing")
    try:
        answer = await asyncio.to_thread(ask_ai_sync, m.text, mode)

        if not await is_pro(m.from_user.id):
            await inc_used_today(m.from_user.id, 1)

        await send_long(m, answer)
    except Exception as e:
        log.exception("text error")
        await m.answer(
            f"⚠️ Xatolik: {type(e).__name__}\n"
            "401 bo‘lsa — GROQ_API_KEY xato.\n"
            "404 bo‘lsa — model nomi xato.\n"
            "Terminaldagi errorni copy qilib yubor."
        )

# -----------------------
# RUN
# -----------------------
async def main():
    await init_db()
    bot = Bot(token=S.bot_token)
    dp = Dispatcher()
    dp.include_router(router)
    log.info("Bot running ✅")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())