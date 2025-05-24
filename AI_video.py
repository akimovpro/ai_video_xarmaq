import os
import logging
import re
import httpx
import openai
import asyncio
import yt_dlp

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, Message
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
from telegram.error import BadRequest as TelegramBadRequest

# -----------------------------------------------------------------------------
# ENVIRONMENT & TOKENS
# -----------------------------------------------------------------------------
BOT_TOKEN      = os.getenv('BOT_TOKEN')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
APP_URL        = os.getenv('APP_URL')
PORT           = int(os.getenv('PORT', '443'))

if not BOT_TOKEN or not OPENAI_API_KEY or not APP_URL:
    raise RuntimeError('BOT_TOKEN, OPENAI_API_KEY, and APP_URL must be set')

openai.api_key = OPENAI_API_KEY

# -----------------------------------------------------------------------------
# SOCKS5 PROXY (yt‑dlp only)
# -----------------------------------------------------------------------------
YTDLP_PROXY_USER = os.getenv('YTDLP_PROXY_USER')
YTDLP_PROXY_PASS = os.getenv('YTDLP_PROXY_PASS')
YTDLP_PROXY_HOST = 'gate.decodo.com'
YTDLP_PROXY_PORT = 7000

if not YTDLP_PROXY_USER or not YTDLP_PROXY_PASS:
    raise RuntimeError('YTDLP_PROXY_USER and YTDLP_PROXY_PASS must be set')

YTDLP_PROXY_URL = (
    f"socks5h://{YTDLP_PROXY_USER}:{YTDLP_PROXY_PASS}"
    f"@{YTDLP_PROXY_HOST}:{YTDLP_PROXY_PORT}"
)

# -----------------------------------------------------------------------------
# LOGGING
# -----------------------------------------------------------------------------
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# USER LANGUAGE PREFERENCES
# -----------------------------------------------------------------------------
user_languages: dict[int, str] = {}

# -----------------------------------------------------------------------------
# REGEX FOR YOUTUBE LINKS
# -----------------------------------------------------------------------------
YOUTUBE_STD_REGEX = re.compile(
    r'(?:https?://)?(?:www\.)?'
    r'(?:youtube\.com/(?:watch\?v=|shorts/|live/|embed/|v/)|youtu\.be/)'
    r'([A-Za-z0-9_-]{11})'
)
YOUTUBE_GOOGLEUSERCONTENT_NUMERIC_REGEX = re.compile(
    r'(https?://(?:www\.)?googleusercontent\.com/youtube\.com/([0-9]+))'
)

# -----------------------------------------------------------------------------
# CAPTION PARSERS (SRT + VTT)
# -----------------------------------------------------------------------------
SRT_PATTERN = re.compile(
    r"^\d+\s*?\n(\d{2}:\d{2}:\d{2},\d{3})\s*-->.*?\n(.+?)\s*?(?:\n\n|\Z)",
    re.S | re.M
)
VTT_TS_RE = re.compile(r"(?P<h>\d{2,}):(?P<m>\d{2}):(?P<s>\d{2})\.(?P<ms>\d{3})")

def _ts2sec(h: int, m: int, s: int) -> int:
    return h * 3600 + m * 60 + s

def parse_srt(text: str) -> list:
    entries = []
    for m in SRT_PATTERN.finditer(text):
        start = m.group(1).split(',')[0]
        h, mi, s = map(int, start.split(':'))
        body = " ".join(line.strip() for line in m.group(2).splitlines() if line.strip())
        if body:
            entries.append({'start': _ts2sec(h, mi, s), 'text': body})
    return entries

def parse_vtt(text: str) -> list:
    entries = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        if '-->' in lines[i]:
            ts = lines[i].split('-->')[0].strip()
            m = VTT_TS_RE.search(ts)
            i += 1
            body_lines = []
            while i < len(lines) and lines[i].strip():
                body_lines.append(lines[i].strip())
                i += 1
            if m and body_lines:
                h, mi, s = int(m.group('h')), int(m.group('m')), int(m.group('s'))
                entries.append({'start': _ts2sec(h, mi, s), 'text': " ".join(body_lines)})
        i += 1
    return entries

def parse_captions(text: str, ext: str) -> list | None:
    return parse_srt(text) if ext == 'srt' else parse_vtt(text) if ext == 'vtt' else None

# -----------------------------------------------------------------------------
# FETCH CAPTIONS WITH yt‑dlp (SOCKS5)
# -----------------------------------------------------------------------------
async def fetch_transcript(video_id_or_url: str, langs: list[str] = None) -> list | None:
    langs = langs or ['ru', 'en']
    ydl_opts = {
        'writesubtitles': True,
        'writeautomaticsub': True,
        'subtitleslangs': langs,
        'subtitlesformat': 'best',
        'skip_download': True,
        'quiet': True,
        'proxy': YTDLP_PROXY_URL,
        'logger': logger,
    }
    loop = asyncio.get_running_loop()
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = await loop.run_in_executor(None, lambda: ydl.extract_info(video_id_or_url, download=False))
    if not info:
        return None
    def pick(pool):
        for lang in langs:
            for ext in ['srt', 'vtt']:
                for it in pool.get(lang, []):
                    if it.get('ext') == ext and it.get('url'):
                        return it['url'], ext
        return None, None
    url, ext = pick(info.get('subtitles', {}))
    if not url:
        url, ext = pick(info.get('automatic_captions', {}))
    if not url:
        return None
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(url)
        r.raise_for_status()
    return parse_captions(r.text, ext)

# -----------------------------------------------------------------------------
# HELPERS
# -----------------------------------------------------------------------------
async def robust_edit(msg: Message | None, text: str, ctx, upd, kb, md=None):
    if msg:
        try:
            await msg.edit_text(text, reply_markup=kb, parse_mode=md)
            return msg
        except TelegramBadRequest:
            pass
    return await ctx.bot.send_message(upd.effective_chat.id, text, reply_markup=kb, parse_mode=md)

# UI keyboards

def main_menu(lang):
    m = {
        'en': ['📺 Summarize Video', '🌐 Change Language', '❓ Help'],
        'ru': ['📺 Аннотировать видео', '🌐 Сменить язык', '❓ Помощь']
    }
    return ReplyKeyboardMarkup([[b] for b in m.get(lang, m['en'])], resize_keyboard=True)

def lang_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton('🇬🇧 English', callback_data='lang_en'),
                                  InlineKeyboardButton('🇷🇺 Русский', callback_data='lang_ru')]])

# -----------------------------------------------------------------------------
# COMMANDS
# -----------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('🎉 *Welcome!* Select language / Выберите язык:', parse_mode='Markdown', reply_markup=lang_kb())

async def language_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    lang = q.data.split('_')[1]
    user_languages[q.from_user.id] = lang
    txt = '🌟 Language set to English!' if lang == 'en' else '🌟 Язык установлен: Русский!'
    await q.message.reply_text(txt, reply_markup=main_menu(lang))

async def language_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('🌐 Select language / Выберите язык:', reply_markup=lang_kb())

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = user_languages.get(update.effective_user.id, 'en')
    en = '1️⃣ Send YouTube link\n2️⃣ Get summary\n3️⃣ /language to change language'
    ru = '1️⃣ Отправьте ссылку YouTube\n2️⃣ Получите аннотацию\n3️⃣ /language для смены языка'
    await update.message.reply_text(en if lang == 'en' else ru, reply_markup=main_menu(lang))

# -----------------------------------------------------------------------------
# MESSAGE HANDLER
# -----------------------------------------------------------------------------
async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = user_languages.get(uid)
    if not lang:
        await update.message.reply_text('Please select a language first using /start or /language.')
        return
    text = update.message.text.strip()
    kb = main_menu(lang)

    if text in ['📺 Summarize Video', '📺 Аннотировать видео']:
        prompt = 'Please send a YouTube link:' if lang == 'en' else 'Отправьте ссылку на видео:'
        await update.message.reply_text(prompt, reply_markup=kb); return
    if text in ['🌐 Change Language', '🌐 Сменить язык']:
        await language_cmd(update, context); return
    if text in ['❓ Help', '❓ Помощь']:
        await help_cmd(update, context); return

    # Extract video id/url
    vid = None
    m = YOUTUBE_STD_REGEX.search(text)
    if m:
        vid = m.group(1)
    else:
        m = YOUTUBE_GOOGLEUSERCONTENT_NUMERIC_REGEX.search(text)
        if m:
            vid = m.group(1)
    if not vid:
        await update.message.reply_text('Invalid YouTube URL.' if lang == 'en' else 'Недействительная ссылка.', reply_markup=kb)
        return

    status = await update.message.reply_text('🔄 Fetching captions…', reply_markup=kb)
    captions = await fetch_transcript(vid)
    if not captions:
        await robust_edit(status, 'Subtitles not found.' if lang == 'en' else 'Субтитры не найдены.', context, update, kb)
        return

    # Build transcript
    transcript = "\n".join(f"[{c['start']//60:02d}:{c['start']%60:02d}] {c['text']}" for c in captions)
    if len(transcript) > 10000:
        transcript = transcript[:10000] + '\n[truncated]'

    instr = (
        'List 3‑7 bullet points (with timestamps) then a 2‑3 paragraph summary.' if lang == 'en'
        else 'Сначала 3‑7 пунктов с таймкодами, затем 2‑3 абзаца пересказа.'
    )
    prompt = f"{instr}\n\nTranscript:\n{transcript}"

    await robust_edit(status, '📝 Summarizing…', context, update, kb)
    try:
        rsp = await openai.ChatCompletion.acreate(
            model='gpt-3.5-turbo',
            messages=[
                {'role': 'system', 'content': 'You summarize video transcripts.'},
                {'role': 'user', 'content': prompt}
            ],
            max_tokens=800,
            temperature=0.5
        )
        summ = rsp.choices[0].message.content.strip()
        await robust_edit(status, summ, context, update, kb, md='Markdown')
    except Exception as e:
        await robust_edit(status, f"OpenAI error: {e}", context, update, kb)

# -----------------------------------------------------------------------------
# ENTRYPOINT
# -----------------------------------------------------------------------------
if __name__ == '__main__':
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('language', language_cmd))
    app.add_handler(CommandHandler('help', help_cmd))
    app.add_handler(CallbackQueryHandler(language_button, pattern='^lang_'))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))

    webhook_path = f"/{BOT_TOKEN.split(':')[-1]}"
    webhook_url  = APP_URL.rstrip('/') + webhook_path
    logger.info(f"Starting webhook at {webhook_url}")

    app.run_webhook(listen='0.0.0.0', port=PORT,	url_path=webhook_path, webhook_url=webhook_url, drop_pending_updates=True)
