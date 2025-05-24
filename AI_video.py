import os
import logging
import re
import httpx
import openai
import asyncio
import yt_dlp
from youtube_transcript_api import (
    YouTubeTranscriptApi,
    TranscriptsDisabled,
    NoTranscriptFound,
)

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Message,
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
BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
APP_URL = os.getenv("APP_URL")
PORT = int(os.getenv("PORT", "443")) # Обычно порт для webhook 443, 80, 88, 8443

# Примерные значения для локального теста, если переменные окружения не установлены
if not BOT_TOKEN:
    # BOT_TOKEN = "YOUR_BOT_TOKEN" # Замените на ваш токен для локального теста
    logging.warning("BOT_TOKEN is not set. Please set it as an environment variable or directly in the code for testing.")
if not OPENAI_API_KEY:
    # OPENAI_API_KEY = "YOUR_OPENAI_API_KEY" # Замените на ваш ключ OpenAI
    logging.warning("OPENAI_API_KEY is not set. Please set it as an environment variable or directly in the code for testing.")
if not APP_URL:
    # APP_URL = "https://your-app-name.herokuapp.com" # Замените на URL вашего вебхука
    logging.warning("APP_URL is not set. Please set it as an environment variable or directly in the code for testing.")


if not BOT_TOKEN or not OPENAI_API_KEY or not APP_URL:
    raise RuntimeError("BOT_TOKEN, OPENAI_API_KEY, and APP_URL must be set for full functionality.")

if OPENAI_API_KEY:
    openai.api_key = OPENAI_API_KEY

# -----------------------------------------------------------------------------
# SOCKS5 PROXY (yt‑dlp only)
# -----------------------------------------------------------------------------
YTDLP_PROXY_USER = os.getenv("YTDLP_PROXY_USER")
YTDLP_PROXY_PASS = os.getenv("YTDLP_PROXY_PASS")
YTDLP_PROXY_HOST = "gate.decodo.com" # Пример, используйте ваш хост
YTDLP_PROXY_PORT = 7000 # Пример, используйте ваш порт

YTDLP_PROXY_URL = None
if YTDLP_PROXY_USER and YTDLP_PROXY_PASS and YTDLP_PROXY_HOST and YTDLP_PROXY_PORT:
    YTDLP_PROXY_URL = (
        f"socks5h://{YTDLP_PROXY_USER}:{YTDLP_PROXY_PASS}"
        f"@{YTDLP_PROXY_HOST}:{YTDLP_PROXY_PORT}"
    )
    logging.info(f"Using yt-dlp proxy: socks5h://{YTDLP_PROXY_USER}:****@{YTDLP_PROXY_HOST}:{YTDLP_PROXY_PORT}")
else:
    logging.warning("YTDLP_PROXY_USER, YTDLP_PROXY_PASS, YTDLP_PROXY_HOST or YTDLP_PROXY_PORT is not set. yt-dlp will run without proxy.")


# -----------------------------------------------------------------------------
# LOGGING
# -----------------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# I18N STRINGS
# -----------------------------------------------------------------------------
T = {
    "start_choose_language": {
        "en": "🎉 *Welcome!* Please choose your language:",
        "ru": "🎉 *Добро пожаловать!* Пожалуйста, выберите язык:",
    },
    "language_set": {
        "en": "🌟 Language set to English!",
        "ru": "🌟 Язык установлен: Русский!",
    },
    "select_language": {"en": "🌐 Select language", "ru": "🌐 Сменить язык"},
    "help_header": {"en": "❓ Help", "ru": "❓ Помощь"},
    "help_text": {
        "en": "1️⃣ Send a YouTube link\n2️⃣ Receive the summary\n3️⃣ Use /language to change language",
        "ru": "1️⃣ Отправьте ссылку на YouTube\n2️⃣ Получите аннотацию\n3️⃣ Используйте /language для смены языка",
    },
    "prompt_send_link": {"en": "📺 Send a YouTube link:", "ru": "📺 Отправьте ссылку на видео:"},
    "invalid_url": {"en": "🚫 Invalid YouTube URL.", "ru": "🚫 Недействительная ссылка."},
    "fetching_captions": {
        "en": "🔄 Fetching captions…",
        "ru": "🔄 Получаем субтитры…",
    },
    "subtitles_not_found": {
        "en": "❌ Subtitles not found.",
        "ru": "❌ Субтитры не найдены.",
    },
    "summarizing": {"en": "📝 Summarizing…", "ru": "📝 Составляем аннотацию…"},
    "openai_error": {"en": "⚠️ OpenAI error:", "ru": "⚠️ Ошибка OpenAI:"},
}

MENU_ITEMS = {
    "summarize": {"en": "📺 Summarize Video", "ru": "📺 Аннотировать видео"},
    "change_lang": {"en": "🌐 Change Language", "ru": "🌐 Сменить язык"},
    "help": {"en": "❓ Help", "ru": "❓ Помощь"},
}


def tr(key: str, lang: str) -> str:
    """Translate helper with graceful fallback to English."""
    return T.get(key, {}).get(lang) or T.get(key, {}).get("en") or key


# -----------------------------------------------------------------------------
# USER LANGUAGE PREFERENCES
# -----------------------------------------------------------------------------
user_languages: dict[int, str] = {}

# -----------------------------------------------------------------------------
# REGEX FOR YOUTUBE LINKS
# -----------------------------------------------------------------------------
YOUTUBE_STD_REGEX = re.compile(
    r"(?:https?://)?(?:www\.)?"
    r"(?:youtube\.com/(?:watch\?v=|shorts/|live/|embed/|v/)|youtu\.be/)"
    r"([A-Za-z0-9_-]{11})",
)
YOUTUBE_GOOGLEUSERCONTENT_NUMERIC_REGEX = re.compile(
    r"(https?://(?:www\.)?googleusercontent\.com/youtube\.com/([0-9]+))",
)

# -----------------------------------------------------------------------------
# CAPTION PARSERS (SRT + VTT)
# -----------------------------------------------------------------------------
SRT_PATTERN = re.compile(
    r"^\d+\s*?\n(\d{2}:\d{2}:\d{2},\d{3})\s*-->.*?\n(.+?)\s*?(?:\n\n|\Z)",
    re.S | re.M,
)
VTT_TS_RE = re.compile(r"(?P<h>\d{2,}):(?P<m>\d{2}):(?P<s>\d{2})\.(?P<ms>\d{3})")


def _ts2sec(h: int, m: int, s: int) -> int:
    return h * 3600 + m * 60 + s


def parse_srt(text: str) -> list:
    entries = []
    for m in SRT_PATTERN.finditer(text):
        start = m.group(1).split(",")[0]
        h, mi, s = map(int, start.split(":"))
        body = " ".join(line.strip() for line in m.group(2).splitlines() if line.strip())
        if body:
            entries.append({"start": _ts2sec(h, mi, s), "text": body})
    return entries


def parse_vtt(text: str) -> list:
    entries = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        if "-->" in lines[i]:
            ts_match = VTT_TS_RE.search(lines[i].split("-->")[0].strip())
            current_line_idx_for_ts = i # Store index for error reporting
            i += 1
            body_lines = []
            while i < len(lines) and lines[i].strip():
                body_lines.append(lines[i].strip())
                i += 1
            if ts_match and body_lines:
                try:
                    h, mi, s = int(ts_match.group("h")), int(ts_match.group("m")), int(ts_match.group("s"))
                    entries.append({"start": _ts2sec(h, mi, s), "text": " ".join(body_lines)})
                except ValueError as e:
                    logger.error(f"Error parsing VTT timestamp parts: {ts_match.group(0)} from line {current_line_idx_for_ts + 1}. Error: {e}")
            elif not ts_match and body_lines:
                logger.warning(f"Found VTT body lines but no valid timestamp match at line {current_line_idx_for_ts +1 } containing text: {lines[current_line_idx_for_ts]}")

        i += 1
    return entries


def parse_captions(text: str, ext: str) -> list | None:
    logger.info(f"Parsing captions with format: {ext}, length: {len(text)}")
    if ext == "srt":
        return parse_srt(text)
    elif ext == "vtt":
        return parse_vtt(text)
    else:
        logger.warning(f"Unknown caption extension: {ext}")
        return None

# -----------------------------------------------------------------------------
# FETCH CAPTIONS WITH MINIMUM TRAFFIC
# -----------------------------------------------------------------------------
async def fetch_transcript(video_id_or_url: str, langs: list[str] | None = None) -> list | None:
    """Return list of dicts with keys start (int seconds) and text (str)."""
    langs = langs or ["ru", "en"]
    logger.info(f"Fetching transcript for: {video_id_or_url}, languages: {langs}")

    video_id_match = YOUTUBE_STD_REGEX.search(video_id_or_url)
    video_id = video_id_match.group(1) if video_id_match else video_id_or_url
    logger.info(f"Normalized Video ID: {video_id}")

    loop = asyncio.get_running_loop()
    try:
        logger.info(f"Attempting to fetch transcript using YouTubeTranscriptApi for video_id: {video_id}")
        transcript_data = await loop.run_in_executor(
            None, lambda: YouTubeTranscriptApi.get_transcript(video_id, languages=langs)
        )
        logger.info(f"YouTubeTranscriptApi: Success. Total entries raw: {len(transcript_data)}")
        if transcript_data:
            logger.debug(f"YouTubeTranscriptApi: Data (first 3 entries): {transcript_data[:3]}")
            logger.debug(f"YouTubeTranscriptApi: Data (last 3 entries): {transcript_data[-3:]}")
        
        processed_transcripts = [
            {"start": int(float(it["start"])), "text": it["text"].replace("\n", " ")}
            for it in transcript_data
            if it.get("text")
        ]
        if processed_transcripts:
            last_caption_api = processed_transcripts[-1]
            logger.info(f"YouTubeTranscriptApi: Processed. Last caption start: {last_caption_api['start'] // 60:02d}:{last_caption_api['start'] % 60:02d}, text: '{last_caption_api['text'][:50]}...'")
        return processed_transcripts
    except (TranscriptsDisabled, NoTranscriptFound) as e:
        logger.warning(f"YouTubeTranscriptApi: Failed for {video_id} ({type(e).__name__}: {e}). Falling back to yt_dlp.")
    except Exception as e:
        logger.error(f"YouTubeTranscriptApi: Unexpected error for {video_id} ({type(e).__name__}: {e}). Falling back to yt_dlp.")


    logger.info("Fallback: Attempting to fetch transcript using yt_dlp.")
    ydl_opts = {
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": langs,
        "subtitlesformat": "best/srv3/ttml/vtt/srt", # Wider range of formats
        "skip_download": True,
        "quiet": True, # Keep this true, use our logger
        "logger": logger, # yt-dlp will use our logger for its messages
        "extract_flat": "in_playlist",
        "cachedir": False,
        "nocheckcertificate": True,
        "extractor_args": {"youtube": {"skip": ["dash", "hls"]}}, # Skip DASH and HLS manifests
        "retries": 3, # Retry downloads
        "socket_timeout": 20, # Timeout for socket operations
    }
    if YTDLP_PROXY_URL:
        ydl_opts["proxy"] = YTDLP_PROXY_URL
    else:
        logger.info("yt_dlp: No proxy configured.")


    def _pick(subs_info, pref_langs, pref_formats=("srt", "vtt", "srv3", "ttml")):
        if not subs_info:
            return None, None
        for lang in pref_langs:
            if lang in subs_info:
                for entry in subs_info[lang]:
                    if entry.get("ext") in pref_formats and entry.get("url"):
                        logger.info(f"yt_dlp _pick: Selected '{lang}' subtitle with format '{entry['ext']}'")
                        return entry["url"], entry["ext"]
        # Fallback: any available language if preferred not found
        for lang_key in subs_info:
            for entry in subs_info[lang_key]:
                 if entry.get("ext") in pref_formats and entry.get("url"):
                    logger.warning(f"yt_dlp _pick: Preferred lang not found. Selected '{lang_key}' subtitle with format '{entry['ext']}'")
                    return entry["url"], entry["ext"]
        return None, None

    info = None
    try:
        logger.info(f"yt_dlp: Extracting info for {video_id_or_url}")
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = await loop.run_in_executor(None, lambda: ydl.extract_info(video_id_or_url, download=False))
    except Exception as e:
        logger.error(f"yt_dlp: Error during extract_info for {video_id_or_url}: {type(e).__name__} - {e}")
        return None

    if not info:
        logger.error(f"yt_dlp: extract_info returned no information for {video_id_or_url}.")
        return None

    # logger.debug(f"yt_dlp info subtitles: {info.get('subtitles')}") # Can be very verbose
    # logger.debug(f"yt_dlp info automatic_captions: {info.get('automatic_captions')}") # Can be very verbose

    url, ext = _pick(info.get("subtitles"), langs)
    if not url:
        logger.info("yt_dlp: No manual subtitles found or matching preferred format. Trying automatic captions.")
        url, ext = _pick(info.get("automatic_captions"), langs)
    
    if not url or not ext:
        logger.warning(f"yt_dlp: No suitable subtitle URL found for {video_id_or_url} with languages {langs}.")
        return None
    
    logger.info(f"yt_dlp: Found subtitle URL: {url} (type: {ext})")

    raw_subtitle_text = None
    async with httpx.AsyncClient(timeout=45.0, headers={"Accept-Encoding": "gzip, deflate"}) as client: # Increased timeout
        try:
            r = await client.get(url)
            r.raise_for_status()
            raw_subtitle_text = r.text
            logger.info(f"yt_dlp: Downloaded '{ext}' subtitles. Total length: {len(raw_subtitle_text)} characters.")
            logger.debug(f"yt_dlp: Subtitles (first 300 chars): '{raw_subtitle_text[:300]}...'")
            logger.debug(f"yt_dlp: Subtitles (last 300 chars): '...{raw_subtitle_text[-300:]}'")
        except httpx.HTTPStatusError as exc:
            logger.error(f"yt_dlp: HTTP error {exc.response.status_code} downloading subtitles from {exc.request.url}: {exc.response.text[:200]}")
            return None
        except httpx.RequestError as exc: # Catches ConnectTimeout, ReadTimeout, etc.
            logger.error(f"yt_dlp: Request error downloading subtitles from {exc.request.url}: {type(exc).__name__} - {exc}")
            return None
        except Exception as e:
            logger.error(f"yt_dlp: Generic error downloading subtitles: {type(e).__name__} - {e}")
            return None
            
    if not raw_subtitle_text:
        logger.error("yt_dlp: raw_subtitle_text is empty after download attempt.")
        return None

    parsed_captions = parse_captions(raw_subtitle_text, ext)
    if parsed_captions:
        logger.info(f"yt_dlp: Parsed captions. Total entries: {len(parsed_captions)}")
        if parsed_captions: # Check again because parse_captions might return empty list
            last_caption_yt_dlp = parsed_captions[-1]
            logger.info(f"yt_dlp: Last parsed caption start: {last_caption_yt_dlp['start'] // 60:02d}:{last_caption_yt_dlp['start'] % 60:02d}, text: '{last_caption_yt_dlp['text'][:50]}...'")
            if last_caption_yt_dlp['start'] < 120 and len(parsed_captions) > 10: 
                 logger.warning("yt_dlp: WARNING - Last caption timestamp from yt_dlp is less than 2 minutes, despite having several entries!")
    else:
        logger.warning(f"yt_dlp: parse_captions returned None or empty list for {ext} format.")
        
    return parsed_captions


# -----------------------------------------------------------------------------
# HELPERS
# -----------------------------------------------------------------------------
async def robust_edit(msg: Message | None, text: str, ctx, upd, kb, md: str | None = None):
    try:
        if msg:
            return await msg.edit_text(text, reply_markup=kb, parse_mode=md)
        else: # msg is None, send a new one
            return await ctx.bot.send_message(upd.effective_chat.id, text, reply_markup=kb, parse_mode=md)
    except TelegramBadRequest as e:
        logger.warning(f"Failed to edit message (likely unchanged or invalid Markdown): {e}. Sending new message.")
        # Fallback to sending a new message if edit fails (e.g. "Message is not modified")
        return await ctx.bot.send_message(upd.effective_chat.id, text, reply_markup=kb, parse_mode=md)
    except Exception as e:
        logger.error(f"Unhandled error in robust_edit: {e}")
        # Fallback for other errors
        try:
            return await ctx.bot.send_message(upd.effective_chat.id, text, reply_markup=kb, parse_mode=md)
        except Exception as e_send:
            logger.error(f"Failed to send fallback message in robust_edit: {e_send}")
            return None


# UI keyboards
def main_menu(lang):
    return ReplyKeyboardMarkup(
        [
            [MENU_ITEMS["summarize"][lang]],
            [MENU_ITEMS["change_lang"][lang]],
            [MENU_ITEMS["help"][lang]],
        ],
        resize_keyboard=True,
    )


def lang_kb():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🇬🇧 English", callback_data="lang_en"),
                InlineKeyboardButton("🇷🇺 Русский", callback_data="lang_ru"),
            ]
        ]
    )


# -----------------------------------------------------------------------------
# COMMANDS
# -----------------------------------------------------------------------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        tr("start_choose_language", "en"), # Always show initial choice in both for clarity
        parse_mode="Markdown",
        reply_markup=lang_kb(),
    )


async def language_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    lang_code = q.data.split("_")[1]
    user_id = q.from_user.id
    user_languages[user_id] = lang_code
    logger.info(f"User {user_id} set language to {lang_code}")
    await q.message.reply_text(tr("language_set", lang_code), reply_markup=main_menu(lang_code))
    # Optionally, delete the language selection message or edit it
    # await q.delete_message() 
    # await q.edit_message_text(text=tr("language_set", lang_code))


async def language_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = user_languages.get(update.effective_user.id, "en")
    await update.message.reply_text(tr("select_language", lang), reply_markup=lang_kb())


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = user_languages.get(update.effective_user.id, "en")
    await update.message.reply_text(
        f"*{tr('help_header', lang)}*\n\n{tr('help_text', lang)}",
        parse_mode="Markdown",
        reply_markup=main_menu(lang)
    )


# -----------------------------------------------------------------------------
# MESSAGE HANDLER
# -----------------------------------------------------------------------------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    lang = user_languages.get(user_id)

    if not lang:
        logger.info(f"User {user_id} in chat {chat_id} has no language set. Prompting.")
        # Use "en" as a fallback for the prompt itself if no language is selected.
        await update.message.reply_text(tr("start_choose_language", "en"), reply_markup=lang_kb(), parse_mode="Markdown")
        return

    text = update.message.text.strip()
    logger.info(f"User {user_id} (lang: {lang}) in chat {chat_id} sent text: \"{text[:100]}\"")
    current_main_menu_kb = main_menu(lang)

    if text == MENU_ITEMS["summarize"][lang]:
        await update.message.reply_text(tr("prompt_send_link", lang), reply_markup=current_main_menu_kb)
        return
    if text == MENU_ITEMS["change_lang"][lang]:
        await language_cmd(update, context) # This will show inline keyboard
        return
    if text == MENU_ITEMS["help"][lang]:
        await help_cmd(update, context)
        return

    # Extract video id/url
    vid_url_or_id = None
    std_match = YOUTUBE_STD_REGEX.search(text)
    if std_match:
        vid_url_or_id = std_match.group(0) # Pass the full URL to yt-dlp for better context
        logger.info(f"Standard YouTube URL detected: {vid_url_or_id}, extracted ID: {std_match.group(1)}")
    else:
        guc_match = YOUTUBE_GOOGLEUSERCONTENT_NUMERIC_REGEX.search(text)
        if guc_match:
            vid_url_or_id = guc_match.group(1) # This is already a URL-like identifier
            logger.info(f"Google User Content YouTube URL detected: {vid_url_or_id}")
        else: # Assume it could be a raw video ID if no regex matches
            if len(text) == 11 and re.match(r"^[A-Za-z0-9_-]+$", text):
                 vid_url_or_id = text
                 logger.info(f"Assuming raw YouTube Video ID: {vid_url_or_id}")


    if not vid_url_or_id:
        await update.message.reply_text(tr("invalid_url", lang), reply_markup=current_main_menu_kb)
        return

    status_message = await update.message.reply_text(tr("fetching_captions", lang), reply_markup=current_main_menu_kb)
    
    captions = None
    try:
        captions = await fetch_transcript(vid_url_or_id, langs=[lang, "en", "ru"]) # Prioritize user's lang, then en, then ru
    except Exception as e:
        logger.error(f"Unhandled exception during fetch_transcript call for {vid_url_or_id}: {type(e).__name__} - {e}", exc_info=True)
        await robust_edit(status_message, tr("subtitles_not_found", lang), context, update, current_main_menu_kb)
        return

    if not captions:
        logger.warning(f"No captions ultimately found or processed for video: {vid_url_or_id}")
        await robust_edit(status_message, tr("subtitles_not_found", lang), context, update, current_main_menu_kb)
        return

    logger.info(f"Successfully fetched and parsed captions. Total entries: {len(captions)} for video: {vid_url_or_id}")
    if captions: # Should always be true if we reached here, but for safety
        first_caption = captions[0]
        last_caption = captions[-1]
        logger.info(f"First caption details: [{first_caption['start'] // 60:02d}:{first_caption['start'] % 60:02d}] '{first_caption['text'][:50]}...'")
        logger.info(f"Last caption details: [{last_caption['start'] // 60:02d}:{last_caption['start'] % 60:02d}] '{last_caption['text'][:50]}...'")
        if last_caption['start'] < 120 and len(captions) > 5: # If many segments but still less than 2 mins
            logger.warning(f"WARNING: The final processed transcript's last caption is at {last_caption['start'] // 60:02d}:{last_caption['start'] % 60:02d}, which is less than 2 minutes.")
        else:
            logger.info(f"Final processed transcript seems to extend beyond 2 minutes. Last timestamp: {last_caption['start'] // 60:02d}:{last_caption['start'] % 60:02d}")


    transcript_parts = []
    for c in captions:
        minutes = c['start'] // 60
        seconds = c['start'] % 60
        transcript_parts.append(f"[{minutes:02d}:{seconds:02d}] {c['text']}")
    
    transcript = "\n".join(transcript_parts)
    
    # Truncate very long transcripts before sending to OpenAI to save tokens & cost
    # Max context for gpt-4.1 can be much larger, but let's be cautious with API costs
    # and processing time. 10k chars is roughly 2k-2.5k tokens.
    MAX_TRANSCRIPT_CHARS = 15000 # Increased slightly
    if len(transcript) > MAX_TRANSCRIPT_CHARS:
        transcript = transcript[:MAX_TRANSCRIPT_CHARS] + f"\n[...transcript truncated at {MAX_TRANSCRIPT_CHARS} characters...]"
        logger.warning(f"Transcript was truncated to {MAX_TRANSCRIPT_CHARS} characters before sending to OpenAI.")
    
    logger.info(f"Transcript length for OpenAI: {len(transcript)} characters.")

    instr = (
        "You are an expert video summarizer. Provide a detailed summary of the following video transcript. "
        "First, list 5-10 key bullet points with timestamps (e.g., [HH:MM:SS] or [MM:SS]) highlighting the main topics. "
        "Then, write a comprehensive 2-4 paragraph summary of the entire provided transcript content. "
        "Preserve maximum details and important information. If the transcript is very short, summarize what is available."
        if lang == "en"
        else "Ты эксперт по аннотированию видео. Сделай подробную аннотацию следующего транскрипта видео. "
        "Сначала напиши 5-10 ключевых пунктов (буллет-пойнтов) с таймкодами (например, [ЧЧ:ММ:СС] или [ММ:СС]), выделяя основные темы. "
        "Затем напиши подробный пересказ всего предоставленного транскрипта в 2-4 абзацах. "
        "Сохрани максимум деталей и важной информации. Если транскрипт очень короткий, сделай аннотацию того, что есть."
    )
    prompt = f"{instr}\n\nVideo Transcript:\n{transcript}"
    # logger.debug(f"OpenAI Prompt: {prompt}") # Can be very long

    await robust_edit(status_message, tr("summarizing", lang), context, update, current_main_menu_kb)
    
    if not OPENAI_API_KEY:
        logger.error("OpenAI API key is not configured. Cannot summarize.")
        await robust_edit(status_message, "OpenAI API key not configured. Summarization unavailable.", context, update, current_main_menu_kb)
        return

    try:
        logger.info("Sending request to OpenAI ChatCompletion...")
        # Consider using a newer model if available, or gpt-3.5-turbo for cost/speed
        # gpt-4.1 is not a standard model name. Common ones: gpt-4, gpt-4-turbo-preview, gpt-3.5-turbo
        # Using gpt-3.5-turbo as a more common and faster alternative for this example
        response = await openai.ChatCompletion.acreate(
            model="gpt-4.1", # Changed to a more common model, adjust if you have access to gpt-4.1 specifically
            messages=[
                {
                    "role": "system",
                    "content": "You are a highly skilled video summarization assistant. You are precise and detailed.",
                },
                {"role": "user", "content": prompt},
            ],
            max_tokens=1000, # Adjusted for potentially detailed summaries
            temperature=0.5,
        )
        summary_text = response.choices[0].message.content.strip()
        logger.info(f"OpenAI response received. Summary length: {len(summary_text)} characters.")
        await robust_edit(status_message, summary_text, context, update, current_main_menu_kb, md="Markdown")
    except openai.APIError as e: # More specific OpenAI error handling
        logger.error(f"OpenAI API Error: {type(e).__name__} - {e}", exc_info=True)
        error_message = f"{tr('openai_error', lang)} API Error: {e}"
        await robust_edit(status_message, error_message, context, update, current_main_menu_kb)
    except Exception as e:
        logger.error(f"Generic error during OpenAI call: {type(e).__name__} - {e}", exc_info=True)
        error_message = f"{tr('openai_error', lang)} {type(e).__name__}"
        await robust_edit(status_message, error_message, context, update, current_main_menu_kb)


# -----------------------------------------------------------------------------
# ENTRYPOINT
# -----------------------------------------------------------------------------
def main():
    if not BOT_TOKEN:
        logger.critical("BOT_TOKEN is not set. The bot cannot start.")
        return
    if not APP_URL: # Needed for webhook
        logger.critical("APP_URL is not set. Webhook cannot be configured.")
        # You might want to allow running with polling if APP_URL is not set,
        # but the current setup is for webhook only.
        return

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("language", language_cmd))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CallbackQueryHandler(language_button_callback, pattern="^lang_"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    webhook_path = f"/{BOT_TOKEN.split(':')[-1]}" # Use a part of the token for uniqueness
    full_webhook_url = APP_URL.rstrip("/") + webhook_path
    
    logger.info(f"Starting webhook: listening on 0.0.0.0:{PORT}, path: {webhook_path}, webhook URL: {full_webhook_url}")

    # For webhook deployment
    application.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=webhook_path, # The path part of your webhook URL
        webhook_url=full_webhook_url, # The full URL telegram will send updates to
        drop_pending_updates=True,
    )

    # For local development with polling (uncomment to use, and comment out run_webhook)
    # logger.info("Starting bot with polling...")
    # application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
