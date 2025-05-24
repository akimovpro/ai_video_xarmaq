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

# Load secrets from environment variables
BOT_TOKEN = os.getenv('BOT_TOKEN')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
APP_URL = os.getenv('APP_URL')  # For keep-alive ping

if not BOT_TOKEN or not OPENAI_API_KEY:
    raise RuntimeError('BOT_TOKEN and OPENAI_API_KEY must be set')

# Initialize OpenAI
openai.api_key = OPENAI_API_KEY

# Logging
logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# User language preference
user_languages = {}

# YouTube ID regex
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
    kb = [[InlineKeyboardButton('🇬🇧 English', callback_data='lang_en'),
           InlineKeyboardButton('🇷🇺 Русский', callback_data='lang_ru')]]
    return InlineKeyboardMarkup(kb)

# Handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        '🎉 *Welcome to YouTube Summarizer!* 🎉\nSelect language / Выберите язык:',
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
    await update.message.reply_text('🌐 *Select language / Выберите язык:*',
                                    parse_mode='Markdown', reply_markup=get_lang_keyboard())

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = user_languages.get(uid, 'en')
    menu = get_main_menu(lang)
    if lang=='en':
        text = ('1. Send a YouTube link\n'
                '2. Get timecoded bullet points + narrative summary\n'
                '3. /language to switch')
    else:
        text = ('1. Отправьте ссылку на YouTube\n'
                '2. Получите таймкоды + связный пересказ\n'
                '3. /language для смены')
    await update.message.reply_text(text, parse_mode='Markdown', reply_markup=menu)

# Transcript fetch
def fetch_transcript(vid: str):
    try:
        return YouTubeTranscriptApi.get_transcript(vid)
    except Exception as e:
        logger.warning(f'Transcript error: {e}')
        return None

# Main message handler
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = user_languages.get(uid)
    text = update.message.text.strip()
    menu = get_main_menu(lang) if lang else None

    if not lang:
        return await update.message.reply_text('Please /start and select language first.')
    if text in ['📺 Summarize Video','📺 Аннотировать видео']:
        prompt = 'Send YouTube link:' if lang=='en' else 'Отправьте ссылку:'
        return await update.message.reply_text(prompt, reply_markup=menu)
    if text in ['🌐 Change Language','🌐 Сменить язык']:
        return await language_cmd(update, context)
    if text in ['❓ Help','❓ Помощь']:
        return await help_cmd(update, context)

    m = re.search(YOUTUBE_REGEX, text)
    if not m:
        err = 'Invalid URL.' if lang=='en' else 'Недействительная ссылка.'
        return await update.message.reply_text(err, reply_markup=menu)
    vid = m.group(1)

    await update.message.reply_text('Processing...⏳' if lang=='en' else 'Обработка...⏳', reply_markup=menu)
    trans = fetch_transcript(vid)
    if not trans:
        msg = 'No transcript.' if lang=='en' else 'Субтитры недоступны.'
        return await update.message.reply_text(msg, reply_markup=menu)

    # Build transcript text
    segs = []
    for e in trans:
        s = int(e['start']); t = e['text']
        mns, secs = divmod(s, 60)
        segs.append(f"[{mns:02d}:{secs:02d}] {t}")
    full = "\n".join(segs)

    # AI prompt
    if lang=='en':
        instr = ('List key bullet points with timestamps. ' 
                 'Then 2-3 paragraph summary starting each with timestamp.')
    else:
        instr = ('Сначала пункты с таймкодами. ' 
                 'Затем 2-3 абзаца пересказа с таймкодами.')
    prompt = instr + "\n\n" + full

    try:
        res = openai.ChatCompletion.create(
            model='gpt-3.5-turbo',
            messages=[{'role':'user','content':prompt}], max_tokens=600
        )
        out = res.choices[0].message.content
    except Exception as e:
        logger.error(f'AI error: {e}')
        return await update.message.reply_text('Error.', reply_markup=menu)

    await update.message.reply_text(out, parse_mode='Markdown', reply_markup=menu)

# Keep-alive ping
def ping():
    while True:
        if APP_URL:
            try: httpx.get(APP_URL)
            except: pass
        time.sleep(30)

# Run bot
if __name__=='__main__':
    threading.Thread(target=ping, daemon=True).start()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('language', language_cmd))
    app.add_handler(CommandHandler('help', help_cmd))
    app.add_handler(CallbackQueryHandler(language_button, pattern='^lang_'))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling()
