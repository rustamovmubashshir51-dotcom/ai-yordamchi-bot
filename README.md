# Ai_yordamchi-bot

SmartYordam AI is a production-grade Telegram bot designed to help students with their homework (Math, English, Essays, Summaries) using the Groq API (OpenAI compatible).

## Features
- **AI Models**: Uses `openai/gpt-oss-20b` (or other Groq models) for fast, smart answers.
- **Photo to Text (OCR)**: Users can send a photo of their homework. The bot extracts text and solves it!
- **Daily Limits & PRO**: Free users get a customizable number of requests per day. PRO users get unlimited.
- **Referral System**: Invite friends to get free PRO days.
- **Admin Commands**: Track usage and grant PRO to users.

## Prerequisites
- Windows OS
- Python 3.10+
- Tesseract OCR (Optional, but required for photo solving)

### Installing Tesseract OCR (Windows)
1. Download Tesseract from [UB-Mannheim/tesseract](https://github.com/UB-Mannheim/tesseract/wiki).
2. Install it (default path is usually `C:\Program Files\Tesseract-OCR\tesseract.exe`).
3. Note the path to the executable.

## Setup & Installation

Follow these exact commands to run the bot on Windows:

```cmd
# 1. Create a virtual environment
python -m venv .venv

# 2. Activate the virtual environment
.venv\Scripts\activate

# 3. Install required packages
pip install -r requirements.txt

# 4. Configure environment variables
copy .env.example .env
```

Open `.env` in your text editor and fill in your keys:
- `BOT_TOKEN`: From [@BotFather](https://t.me/BotFather)
- `GROQ_API_KEY`: From [Groq Console](https://console.groq.com)
- `TESSERACT_CMD`: Path to `tesseract.exe` (if installed)
Wait, edit the `.env` file first and make sure that you provide your correct API Keys before starting the bot.

## Running the Bot

```cmd
# Ensure virtual environment is activated, then run:
python -m app.main
```

## Common Errors & Fixes

### 1. Model Not Found (`openai.NotFoundError`)
- **Symptom**: The AI fails to respond or logs show `model not found`.
- **Fix**: Check `GROQ_MODEL` and `GROQ_MODEL_FALLBACK` in `.env`. Groq regularly updates their models. Make sure you are using a currently supported model name (e.g., `llama3-8b-8192` or `mixtral-8x7b-32768`).

### 2. Invalid Token (`aiogram.exceptions.TelegramUnauthorizedError`)
- **Symptom**: Error when starting the bot: `Unauthorized: invalid token`.
- **Fix**: Double check your `BOT_TOKEN` in the `.env` file. Do not include spaces or quotes around the token.

### 3. Missing Tesseract (`tesseract_not_installed`)
- **Symptom**: When sending a photo, the bot replies that OCR is not installed.
- **Fix**: Ensure Tesseract is installed and the `TESSERACT_CMD` in `.env` points exactly to the executable (e.g. `TESSERACT_CMD=C:\Program Files\Tesseract-OCR\tesseract.exe`). Or turn off OCR by setting `ENABLE_OCR=0`.

### 4. Database Migration Issues
- **Symptom**: `OperationalError: no such column...`
- **Fix**: The bot automatically attempts to add new columns to an existing SQLite DB on startup. If the database gets corrupted, you can simply delete `bot.db` to start fresh (WARNING: This deletes all user data and PRO statuses).
