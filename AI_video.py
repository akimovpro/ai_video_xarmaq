import os
import logging
import re
import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
from youtube_transcript_api import YouTubeTranscriptApi, TranscriptsDisabled, NoTranscriptFound
import openai

# Load secrets from environment variables
BOT_TOKEN = os.getenv('BOT_TOKEN')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
APP_URL = os.getenv('APP_URL')  # For keep-alive ping

# Validate environment variables
if not BOT_TOKEN or not OPENAI_API_KEY:
    raise RuntimeError('Environment variables BOT_TOKEN and OPENAI_API_KEY must be set')

# Initialize OpenAI client
openai.api_key = OPENAI_API_KEY

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# In-memory user language prefs
user_languages = {}

# Regex for YouTube IDs
YOUTUBE_REGEX = r"(?:https?://)?(?:www\.)?(?:youtube\.com/(?:watch\?v=|shorts/)|youtu\.be/)([A-Za-z0-9_-]{11})"

# Keyboards
def get_main_menu(lang: str) -> ReplyKeyboardMarkup:
    labels = {
        'en': ['📺 Summarize Video', '🌐 Change Language', '❓ Help'],
        'ru': ['📺 Аннотировать видео', '🌐 Сменить язык', '❓ Помощь'],
    }
    buttons = [[text] for text in labels.get(lang, labels['en'])]
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)

def get_lang_selection_keyboard() -> InlineKeyboardMarkup:
    keyboard = [[
        InlineKeyboardButton('🇬🇧 English', callback_data='lang_en'),
        InlineKeyboardButton('🇷🇺 Русский', callback_data='lang_ru'),
    ]]
    return InlineKeyboardMarkup(keyboard)

# Handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        '🎉 *Welcome to YouTube Summarizer!* 🎉\nSelect language / Выберите язык:',
        parse_mode='Markdown',
        reply_markup=get_lang_selection_keyboard()
    )

async def language_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = query.data.split('_')[1]
    user_languages[user_id] = lang
    message = '🌟 Language set to English!' if lang=='en' else '🌟 Язык установлен: Русский!'
    await query.message.reply_text(
        message,
        parse_mode='Markdown',
        reply_markup=get_main_menu(lang)
    )
    await query.message.delete()

async def language_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        '🌐 *Select language / Выберите язык:*',
        parse_mode='Markdown',
        reply_markup=get_lang_selection_keyboard()
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    lang = user_languages.get(user_id, 'en')
    menu = get_main_menu(lang)
    if lang == 'en':
        text = (
            '1️⃣ Send a YouTube link to summarize video content.\n'
            '2️⃣ Receive timecoded bullet points AND a cohesive narrative summary.\n'
            '3️⃣ Use /language to switch language.'
        )
    else:
        text = (
            '1️⃣ Отправьте ссылку на YouTube для аннотации видео.\n'
            '2️⃣ Получите таймкоды и связный пересказ.\n'
            '3️⃣ Используйте /language для смены языка.'
        )
    await update.message.reply_text(text, parse_mode='Markdown', reply_markup=menu)

# Fetch transcript by letting library auto-select sources
def fetch_transcript(video_id: str):
    """Fetch transcript, auto-generated if no manual captions."""
    try:
        # get_transcript will return manual or auto-generated captions
        return YouTubeTranscriptApi.get_transcript(video_id)
    except Exception as e:
        logger.error(f"Transcript fetch error: {e}")
        return None

# Handle incoming messages...
if __name__ == '__main__':
    # Start keep-alive thread to prevent sleeping
    import threading, time
    def ping_loop():
        while True:
            if APP_URL:
                try:
                    httpx.get(APP_URL)
                except Exception:
                    pass
            time.sleep(30)
    threading.Thread(target=ping_loop, daemon=True).start()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('language', language_cmd))
    app.add_handler(CommandHandler('help', help_cmd))
    app.add_handler(CallbackQueryHandler(language_button, pattern='^lang_'))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Run polling (ensure only one instance)
    app.run_polling()
