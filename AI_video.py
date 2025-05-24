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
from youtube_transcript_api import YouTubeTranscriptApi
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
def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return update.message.reply_text(
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

# Fetch transcript
async def fetch_transcript(video_id: str):
    try:
        return YouTubeTranscriptApi.get_transcript(video_id)
    except Exception as e:
        logger.error(f"Transcript fetch error: {e}")
        return None

# Handle incoming messages
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()
    lang = user_languages.get(user_id)
    menu = get_main_menu(lang) if lang else None

    if not lang:
        return await update.message.reply_text('Please /start and select language first.')

    # Button actions
    if text in ['📺 Summarize Video','📺 Аннотировать видео']:
        prompt = 'Send YouTube link:' if lang=='en' else 'Отправьте ссылку на YouTube:'
        return await update.message.reply_text(prompt, reply_markup=menu)
    if text in ['🌐 Change Language','🌐 Сменить язык']:
        return await language_cmd(update, context)
    if text in ['❓ Help','❓ Помощь']:
        return await help_cmd(update, context)

    # Extract video ID
    m = re.search(YOUTUBE_REGEX, text)
    if not m:
        err = 'Invalid YouTube URL.' if lang=='en' else 'Недействительная ссылка YouTube.'
        return await update.message.reply_text(err, reply_markup=menu)
    video_id = m.group(1)

    await update.message.reply_text('Processing...⏳' if lang=='en' else 'Обработка...⏳', reply_markup=menu)

    transcript = await fetch_transcript(video_id)
    if not transcript:
        msg = 'Transcript not available.' if lang=='en' else 'Субтитры недоступны.'
        return await update.message.reply_text(msg, reply_markup=menu)

    # Build timecoded text
    segments = []
    for seg in transcript:
        start = seg['start']
        content = seg['text']
        mns, secs = divmod(int(start), 60)
        segments.append(f"[{mns:02d}:{secs:02d}] {content}")
    full_text = "\n".join(segments)

    # Prepare prompt for AI
    if lang=='en':
        instr = (
            'First, list key bullet points with timestamps. '
            'Then provide a concise 2-3 paragraph narrative summary starting each paragraph with its timestamp.'
        )
    else:
        instr = (
            'Сначала список ключевых пунктов с таймкодами. '
            'Затем 2-3 абзаца связного пересказа, каждый абзац с таймкодом.'
        )
    ai_prompt = instr + "\n\n" + full_text

    try:
        resp = openai.ChatCompletion.create(
            model='gpt-3.5-turbo',
            messages=[{'role':'system','content':'You summarize YouTube transcripts.'},{'role':'user','content':ai_prompt}],
            max_tokens=800
        )
        output = resp.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"OpenAI error: {e}")
        return await update.message.reply_text('Error generating summary.', reply_markup=menu)

    await update.message.reply_text(output, parse_mode='Markdown', reply_markup=menu)

# Keep-alive ping thread
def ping_loop():
    while True:
        if APP_URL:
            try:
                httpx.get(APP_URL)
            except:
                pass
        time.sleep(30)

# Entry point
if __name__ == '__main__':
    import threading, time
    threading.Thread(target=ping_loop, daemon=True).start()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('language', language_cmd))
    app.add_handler(CommandHandler('help', help_cmd))
    app.add_handler(CallbackQueryHandler(language_button, pattern='^lang_'))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling()
