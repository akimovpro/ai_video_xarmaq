import os
import logging
import re
import httpx
import openai
import asyncio  # Для запуска блокирующих операций в executor'е
import yt_dlp    # Библиотека для работы с YouTube (субтитры и пр.)

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

openai.api_key = OPENAI_API_KEY   # Настройка ключа (без прокси — он не нужен)

# -----------------------------------------------------------------------------
# SOCKS5 PROXY (только для yt-dlp): логин/пароль берём из переменных окружения
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
# SRT & VTT PARSERS
# -----------------------------------------------------------------------------
SRT_PATTERN = re.compile(
    r"^\d+\s*?\n"  # cue number
    r"(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*"  # start
    r"(\d{2}:\d{2}:\d{2},\d{3})\s*?\n"     # end (не используем)
    r"(.+?)\s*?(\n\n|\Z)",
    re.S | re.M
)

VTT_TS_RE = re.compile(r"(?P<h>\d{2,}):(?P<m>\d{2}):(?P<s>\d{2})\.(?P<ms>\d{3})")

def _timestamp_to_seconds(h: int, m: int, s: int) -> int:
    return h * 3600 + m * 60 + s


def parse_srt_content(text: str, logger_obj=None) -> list | None:
    """Парсит SRT и возвращает [{'start': seconds, 'text': str}, …]."""
    entries = []
    for match in SRT_PATTERN.finditer(text):
        try:
            start_str = match.group(1)
            body      = match.group(3)
            h, m, s = map(int, start_str.split(',')[0].split(':'))
            start_seconds = _timestamp_to_seconds(h, m, s)
            clean_text = " ".join(line.strip() for line in body.strip().splitlines() if line.strip())
            if clean_text:
                entries.append({'start': start_seconds, 'text': clean_text})
        except Exception as e:
            if logger_obj:
                logger_obj.error(f"SRT parse error: {e}")
    return entries or None


def parse_vtt_content(text: str, logger_obj=None) -> list | None:
    """Очень простой VTT‑парсер suficiente для bullet‑summary."""
    lines = text.strip().splitlines()
    entries = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if "-->" in line:
            try:
                ts_str = line.split("-->")[0].strip()
                m = VTT_TS_RE.search(ts_str)
                if not m:
                    i += 1; continue
                h = int(m.group('h'))
                m_ = int(m.group('m'))
                s_ = int(m.group('s'))
                start_seconds = _timestamp_to_seconds(h, m_, s_)
                # Collect text until blank line
                i += 1
                text_lines = []
                while i < len(lines) and lines[i].strip():
                    text_lines.append(lines[i].strip())
                    i += 1
                clean_text = " ".join(text_lines).strip()
                if clean_text:
                    entries.append({'start': start_seconds, 'text': clean_text})
            except Exception as e:
                if logger_obj:
                    logger_obj.error(f"VTT parse error at line {i}: {e}")
        i += 1
    return entries or None

# unified function

def parse_captions(text: str, ext: str, logger_obj=None):
    if ext == 'srt':
        return parse_srt_content(text, logger_obj)
    if ext == 'vtt':
        return parse_vtt_content(text, logger_obj)
    return None

# -----------------------------------------------------------------------------
# FETCH TRANSCRIPT WITH yt-dlp (через SOCKS5-proxy)
# -----------------------------------------------------------------------------
async def fetch_transcript_with_yt_dlp(
    video_url_or_id: str,
    target_langs: list[str] | None = None,
    logger_obj=logger
) -> list | None:
    target_langs = target_langs or ['ru', 'en']

    if logger_obj:
        logger_obj.info(
            f"yt-dlp: Запрос субтитров для '{video_url_or_id}' (langs={target_langs})"
        )

    ydl_opts = {
        'writesubtitles'    : True,
        'writeautomaticsub' : True,
        'subtitleslangs'    : target_langs,
        'subtitlesformat'   : 'best',       # пусть выбирает srt/vtt
        'skip_download'     : True,
        'quiet'             : True,
        'noplaylist'        : True,
        'noprogress'        : True,
        'logger'            : logger_obj,
        'extract_flat'      : 'in_playlist',
        'ignoreerrors'      : True,
        'proxy'             : YTDLP_PROXY_URL,
    }

    try:
        loop = asyncio.get_running_loop()
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info_dict = await loop.run_in_executor(
                None, lambda: ydl.extract_info(video_url_or_id, download=False)
            )

        if not info_dict:
            logger_obj.warning("yt-dlp: extract_info вернул None")
            return None

        logger_obj.info(
            f"yt-dlp: Обработано видео '{info_dict.get('title', 'N/A')}' (ID: {info_dict.get('id')})"
        )

        # Подбираем субтитры: сначала SRT, затем VTT
        def find_caption(lang_pool: dict, preferred_exts):
            for lang in target_langs:
                for ext in preferred_exts:
                    for item in lang_pool.get(lang, []):
                        if item.get('ext') == ext and item.get('url'):
                            return item['url'], ext
            return None, None

        url, ext = find_caption(info_dict.get('subtitles', {}), ['srt', 'vtt'])
        if not url:
            url, ext = find_caption(info_dict.get('automatic_captions', {}), ['srt', 'vtt'])

        if not url:
            logger_obj.warning("yt-dlp: Subtitles not found in any supported format (srt/vtt)")
            return None

        logger_obj.info(f"yt-dlp: Загрузка {ext.upper()} субтитров: {url[:100]}…")

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            captions_text = resp.text

        return parse_captions(captions_text, ext, logger_obj)

    except yt_dlp.utils.DownloadError as e:
        logger_obj.error(f"yt-dlp DownloadError: {e}")
    except httpx.HTTPStatusError as e:
        logger_obj.error(f"HTTPStatusError при скачивании субтитров: {e}")
    except Exception as e:
        logger_obj.error(f"Общая ошибка yt-dlp: {type(e).__name__}: {e}")
    return None

# -----------------------------------------------------------------------------
# ROBUST EDIT (без изменений)
# -----------------------------------------------------------------------------
async def robust_edit_text(
    message_to_edit: Message | None,
    new_text: str,
    context: ContextTypes.DEFAULT_TYPE,
    update_for_fallback: Update,
    reply_markup: InlineKeyboardMarkup | ReplyKeyboardMarkup | None,
    parse_mode: str | None = None
) -> Message | None:
    if message_to_edit:
        try:
            await message_to_edit.edit_text(
                new_text, reply_markup=reply_markup, parse_mode=parse_mode
            )
            return message_to_edit
        except TelegramBadRequest as e:
            if "Message is not modified" in str(e):
                logger.info(f"Message {message_to_edit.message_id} not modified.")
                return message_to_edit
            logger.warning(
                f"Failed to edit message {message_to_edit.message_id} (error: {e}). Sending new."
            )
        except Exception as e:
            logger.error(
                f"Unexpected error editing {message_to_edit.message_id}: {e}. Sending new."
            )
    else:
        logger.warning("robust_edit_text called with None message_to_edit. Sending new.")

    try:
        return await context.bot.send_message(
            chat_id=update_for_fallback.effective_chat.id,
            text=new_text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )
    except Exception as e_send:
        logger.error(f"Failed to send fallback message: {e_send}")
        return None

# -----------------------------------------------------------------------------
# UI HELPERS
# -----------------------------------------------------------------------------

def get_main_menu(lang: str) -> ReplyKeyboardMarkup:
    labels = {
        'en': ['📺 Summarize Video', '🌐 Change Language', '❓ Help'],
        'ru': ['📺 Аннотировать видео', '🌐 Сменить язык', '❓ Помощь'],
    }
    return ReplyKeyboardMarkup(
        [[lbl] for lbl in labels.get(lang, labels['en'])], resize_keyboard=True
    )


def get_lang_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton('🇬🇧 English', callback_data='lang_en'),
        InlineKeyboardButton('🇷🇺 Русский', callback_data='lang_ru'),
    ]])

# -----------------------------------------------------------------------------
# COMMAND HANDLERS
# -----------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        '🎉 *Welcome!* Select language / Выберите язык:',
        parse_mode='Markdown', reply_markup=get_lang_keyboard()
    )

async def language_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    lang = q.data.split('_')[1]
    user_languages[q.from_user.id] = lang
    msg = '🌟 Language set to English!' if lang == 'en' else '🌟 Язык установлен: Русский!'
    await q.message.reply_text(msg, parse_mode='Markdown', reply_markup=get_main_menu(lang))

async def language_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        '🌐 Select language / Выберите язык:',
        parse_mode='Markdown',
        reply_markup=get_lang_keyboard()
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = user_languages.get(update.effective_user.id, 'en')
    text_en = ('1️⃣ Send YouTube link (standard or googleusercontent.com/youtube.com/NUMERIC_ID format)\n'
               '2️⃣ Receive bullet points + narrative summary\n'
               '3️⃣ /language to change language')
    text_ru = ('1️⃣ Отправьте ссылку YouTube (стандартного формата или googleusercontent.com/youtube.com/ЧИСЛОВОЙ_ID)\n'
               '2️⃣ Получите пункты + пересказ\n'
               '3️⃣ /language для смены языка')
    await update.message.reply_text(
        text_en if lang == 'en' else text_ru,
        parse_mode='Markdown',
        reply_markup=get_main_menu(lang)
    )

# -----------------------------------------------------------------------------
# MESSAGE HANDLER
# -----------------------------------------------------------------------------

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    lang = user_languages.get(uid)

    if not lang:
        await update.message.reply_text('Please select a language first using /start or /language.')
        return

    text_input = update.message.text.strip()
    menu       = get_main_menu(lang)

    # UI buttons
    if text_input in ['📺 Summarize Video', '📺 Аннотировать видео']:
        await update.message.reply_text(
            'Please send me a YouTube video link to summarize:' if lang == 'en'
            else 'Пожалуйста, отправьте ссылку на видео для аннотации:',
            reply_markup=menu
        ); return
    if text_input in ['🌐 Change Language
