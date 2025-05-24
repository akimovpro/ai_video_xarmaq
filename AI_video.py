import os
import logging
import re
import httpx
import threading
import time
import openai
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
from youtube_transcript_api import YouTubeTranscriptApi
from pytube import YouTube

# Load environment variables
BOT_TOKEN = os.getenv('BOT_TOKEN')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
APP_URL = os.getenv('APP_URL')      # e.g. https://your-app.onrender.com
PORT = int(os.getenv('PORT', '443'))

if not BOT_TOKEN or not OPENAI_API_KEY or not APP_URL:
    raise RuntimeError('BOT_TOKEN, OPENAI_API_KEY, and APP_URL must be set')

# Initialize OpenAI
openai.api_key = OPENAI_API_KEY

# Logging
logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# User language preferences
user_languages = {}
YOUTUBE_REGEX = r'(?:https?://)?(?:www\.)?(?:youtube\.com/(?:watch\?v=|shorts/)|youtu\.be/)([A-Za-z0-9_-]{11})'

# Keyboards
def get_main_menu(lang: str) -> ReplyKeyboardMarkup:
    labels = {
        'en': ['📺 Summarize Video', '🌐 Change Language', '❓ Help'],
        'ru': ['📺 Аннотировать видео', '🌐 Сменить язык', '❓ Помощь'],
    }
    buttons = [[lbl] for lbl in labels.get(lang, labels['en'])]
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)

def get_lang_keyboard() -> InlineKeyboardMarkup:
    kb = [[
        InlineKeyboardButton('🇬🇧 English', callback_data='lang_en'),
        InlineKeyboardButton('🇷🇺 Русский', callback_data='lang_ru'),
    ]]
    return InlineKeyboardMarkup(kb)

# Handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        '🎉 *Welcome!* Select language / Выберите язык:',
        parse_mode='Markdown', reply_markup=get_lang_keyboard()
    )

async def language_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    lang = q.data.split('_')[1]
    user_languages[uid] = lang
    msg = '🌟 Language set to English!' if lang=='en' else '🌟 Язык установлен: Русский!'
    await q.message.reply_text(msg, parse_mode='Markdown', reply_markup=get_main_menu(lang))
    await q.message.delete()

async def language_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        '🌐 Select language / Выберите язык:', parse_mode='Markdown', reply_markup=get_lang_keyboard()
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = user_languages.get(uid, 'en')
    menu = get_main_menu(lang)
    if lang=='en':
        text = '1️⃣ Send YouTube link\n2️⃣ Receive bullet points + narrative summary\n3️⃣ /language'
    else:
        text = '1️⃣ Отправьте ссылку\n2️⃣ Получите пункты + пересказ\n3️⃣ /language'
    await update.message.reply_text(text, parse_mode='Markdown', reply_markup=menu)

# Fetch transcript with pytube fallback
def fetch_transcript(video_id: str):
    # Try youtube_transcript_api first
    try:
        return YouTubeTranscriptApi.get_transcript(video_id)
    except Exception:
        pass
    # Fallback to pytube captions
    try:
        url = f'https://www.youtube.com/watch?v={video_id}'
        yt = YouTube(url)
        # prefer manual then auto
        cap = yt.captions.get_by_language_code('en') or yt.captions.get_by_language_code('ru')
        if not cap:
            cap = yt.captions.get_by_language_code('a.en') or yt.captions.get_by_language_code('a.ru')
        if not cap:
            return None
        srt = cap.generate_srt_captions()
        entries = []
        import re as _re
        pattern = _re.compile(r"(\d+)\n(\d{2}):(\d{2}):(\d{2}),(\d{3}) --> .*?\n(.*?)(?=(?:\n\n|$))", _re.S)
        for m in pattern.finditer(srt):
            h, mn, s = int(m.group(2)), int(m.group(3)), int(m.group(4))
            start = h*3600 + mn*60 + s
            text = m.group(6).replace('\n', ' ')
            entries.append({'start': start, 'text': text})
        return entries
    except Exception as e:
        logger.warning(f'Pytube fallback error: {e}')
        return None

# Main message handler
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = user_languages.get(uid)
    text = update.message.text.strip()
    menu = get_main_menu(lang) if lang else None

    if not lang:
        return await update.message.reply_text('Please /start first.')
    if text in ['📺 Summarize Video','📺 Аннотировать видео']:
        return await update.message.reply_text(
            'Send YouTube link:' if lang=='en' else 'Отправьте ссылку:', reply_markup=menu)
    if text in ['🌐 Change Language','🌐 Сменить язык']:
        return await language_cmd(update, context)
    if text in ['❓ Help','❓ Помощь']:
        return await help_cmd(update, context)

    m = re.search(YOUTUBE_REGEX, text)
    if not m:
        return await update.message.reply_text(
            'Invalid URL.' if lang=='en' else 'Недействительная ссылка.', reply_markup=menu)
    vid = m.group(1)
    await update.message.reply_text('Processing...' if lang=='en' else 'Обработка...', reply_markup=menu)
    trans = fetch_transcript(vid)
    if not trans:
        return await update.message.reply_text(
            'No transcript.' if lang=='en' else 'Субтитры недоступны.', reply_markup=menu)

    parts = [f"[{e['start']//60:02d}:{e['start']%60:02d}] {e['text']}" for e in trans]
    full = "\n".join(parts)
    instr = (
        'List key bullet points with timestamps, then a concise 2-3 paragraph summary starting each with timestamp.'
        if lang=='en' else
        'Сначала пункты с таймкодами, затем 2-3 абзаца пересказа с таймкодами.'
    )
    prompt = instr + "\n\n" + full
    resp = openai.ChatCompletion.create(
        model='gpt-3.5-turbo',
        messages=[{'role':'system','content':'You summarize transcripts.'}, {'role':'user','content':prompt}],
        max_tokens=700
    )
    await update.message.reply_text(resp.choices[0].message.content, parse_mode='Markdown', reply_markup=menu)

# Webhook entry
if __name__=='__main__':
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('language', language_cmd))
    app.add_handler(CommandHandler('help', help_cmd))
    app.add_handler(CallbackQueryHandler(language_button, pattern='^lang_'))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    webhook_path = f"/bot{BOT_TOKEN}"
    webhook_url = f"{APP_URL}{webhook_path}"
    logger.info(f"Starting webhook at {webhook_url}")
    app.run_webhook(
        listen='0.0.0.0',
        port=PORT,
        url_path=webhook_path,
        webhook_url=webhook_url,
        drop_pending_updates=True
    )
