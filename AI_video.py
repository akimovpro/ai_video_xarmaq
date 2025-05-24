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
# SOCKS5 PROXY (только для yt-dlp)
# -----------------------------------------------------------------------------
YTDLP_PROXY_USER = 'user-spjjpiibpj-session-1'
YTDLP_PROXY_PASS = 'gG4W=ar8fgVy3uK3lx'
YTDLP_PROXY_HOST = 'gate.decodo.com'
YTDLP_PROXY_PORT = 7000

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
# SRT PARSER
# -----------------------------------------------------------------------------
def parse_srt_content(srt_text: str, logger_obj=None) -> list | None:
    """Парсит SRT-контент и возвращает список словарей {'start': секунды, 'text': строка}."""
    entries = []
    pattern = re.compile(
        r"^\d+\s*?\n"
        r"(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})\s*?\n"
        r"(.+?)\s*?(\n\n|\Z)",
        re.S | re.M
    )
    for match in pattern.finditer(srt_text):
        try:
            start_time_str = match.group(1)
            raw_text_block = match.group(3)

            text_lines = [
                line.strip() for line in raw_text_block.strip().splitlines() if line.strip()
            ]
            text_content = " ".join(text_lines)

            h, m, s = map(int, start_time_str.split(',')[0].split(':'))
            start_seconds = h * 3600 + m * 60 + s

            if text_content:
                entries.append({'start': start_seconds, 'text': text_content})
        except Exception as e:
            if logger_obj:
                logger_obj.error(
                    f"Ошибка парсинга SRT блока: "
                    f"'{match.group(0)[:150].replace(chr(10), ' ')}...' -> {e}"
                )
            continue

    if not entries and srt_text and logger_obj:
        logger_obj.warning("SRT контент был, но парсинг не дал записей. Проверьте формат.")
    return entries or None

# -----------------------------------------------------------------------------
# FETCH TRANSCRIPT WITH yt-dlp (через SOCKS5-proxy)
# -----------------------------------------------------------------------------
async def fetch_transcript_with_yt_dlp(
    video_url_or_id: str,
    target_langs: list[str] = ['ru', 'en'],
    logger_obj=logger
) -> list | None:
    """
    Получает субтитры видео (SRT) через yt-dlp + SOCKS5-proxy.
    """
    if logger_obj:
        logger_obj.info(
            f"yt-dlp: Запрос субтитров для '{video_url_or_id}' (langs={target_langs})"
        )

    ydl_opts = {
        'writesubtitles'    : True,
        'writeautomaticsub' : True,
        'subtitleslangs'    : target_langs,
        'subtitlesformat'   : 'srt',
        'skip_download'     : True,
        'quiet'             : True,
        'noplaylist'        : True,
        'noprogress'        : True,
        'logger'            : logger_obj,
        'extract_flat'      : 'in_playlist',
        'ignoreerrors'      : True,
        'proxy'             : YTDLP_PROXY_URL,   # ← SOCKS5-прокси только для yt-dlp
    }

    try:
        loop = asyncio.get_running_loop()
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info_dict = await loop.run_in_executor(
                None, lambda: ydl.extract_info(video_url_or_id, download=False)
            )

        if not info_dict:
            if logger_obj:
                logger_obj.warning("yt-dlp: extract_info вернул None")
            return None

        video_id_extracted = info_dict.get('id', 'N/A')
        if logger_obj:
            logger_obj.info(
                f"yt-dlp: Обработано видео '{info_dict.get('title', 'N/A')}' (ID: {video_id_extracted})"
            )

        chosen_sub_url = None
        chosen_lang_type = ""

        for lang_code in target_langs:
            # Сначала ручные субтитры
            for sub_dict in info_dict.get('subtitles', {}).get(lang_code, []):
                if sub_dict.get('ext') == 'srt' and sub_dict.get('url'):
                    chosen_sub_url = sub_dict['url']
                    chosen_lang_type = "manual"
                    break
            if chosen_sub_url:
                break
            # Затем автоматические
            for sub_dict in info_dict.get('automatic_captions', {}).get(lang_code, []):
                if sub_dict.get('ext') == 'srt' and sub_dict.get('url'):
                    chosen_sub_url = sub_dict['url']
                    chosen_lang_type = "auto"
                    break
            if chosen_sub_url:
                break

        if not chosen_sub_url:
            if logger_obj:
                logger_obj.warning("yt-dlp: SRT субтитры не найдены")
            return None

        if logger_obj:
            logger_obj.info(f"yt-dlp: Загрузка {chosen_lang_type} SRT: {chosen_sub_url[:100]}")

        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(chosen_sub_url)
            response.raise_for_status()
            srt_content = response.text

        return parse_srt_content(srt_content, logger_obj)

    except yt_dlp.utils.DownloadError as e:
        if logger_obj:
            logger_obj.error(f"yt-dlp DownloadError: {e}")
        return None
    except httpx.HTTPStatusError as e:
        if logger_obj:
            logger_obj.error(f"HTTPStatusError при скачивании субтитров: {e}")
        return None
    except Exception as e:
        if logger_obj:
            logger_obj.error(f"Общая ошибка yt-dlp: {type(e).__name__} - {e}")
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
    return ReplyKeyboardMarkup([[lbl] for lbl in labels.get(lang, labels['en'])], resize_keyboard=True)

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

    # UI buttons routed
    if text_input in ['📺 Summarize Video', '📺 Аннотировать видео']:
        msg = ('Please send me a YouTube video link to summarize:'
               if lang == 'en'
               else 'Пожалуйста, отправьте ссылку на видео для аннотации:')
        await update.message.reply_text(msg, reply_markup=menu)
        return
    if text_input in ['🌐 Change Language', '🌐 Сменить язык']:
        await language_cmd(update, context);  return
    if text_input in ['❓ Help', '❓ Помощь']:
        await help_cmd(update, context);      return

    # Try to extract YouTube ID / URL
    video_url_or_id_for_yt_dlp: str | None = None
    std_match = YOUTUBE_STD_REGEX.search(text_input)
    if std_match:
        video_url_or_id_for_yt_dlp = std_match.group(1)
    else:
        guc_match = YOUTUBE_GOOGLEUSERCONTENT_NUMERIC_REGEX.search(text_input)
        if guc_match:
            video_url_or_id_for_yt_dlp = guc_match.group(1)

    if not video_url_or_id_for_yt_dlp:
        msg = 'Invalid YouTube URL.' if lang == 'en' else 'Недействительная ссылка YouTube.'
        await update.message.reply_text(msg, reply_markup=menu)
        return

    status_msg = await update.message.reply_text(
        'Processing the video...' if lang == 'en' else 'Обрабатываю видео...',
        reply_markup=menu
    )

    # Fetch transcript through yt-dlp (goes via SOCKS5)
    transcript = await fetch_transcript_with_yt_dlp(
        video_url_or_id_for_yt_dlp,
        logger_obj=logger
    )

    if not transcript:
        await robust_edit_text(
            status_msg,
            'Sorry, I could not retrieve subtitles for this video.' if lang == 'en'
            else 'Не удалось получить субтитры для этого видео.',
            context, update, menu
        )
        return

    # Build plain-text transcript
    parts = [
        f"[{entry['start'] // 60:02d}:{entry['start'] % 60:02d}] {entry['text']}"
        for entry in transcript
    ]
    full_transcript_text = "\n".join(parts)
    if len(full_transcript_text) > 10000:
        full_transcript_text = full_transcript_text[:10000] + "\n[Transcript truncated …]"

    prompt_header = (
        'List key bullet points (3-7) with timestamps, then a concise 2-3 paragraph summary '
        'starting each with timestamp.\n\nTranscript:\n' if lang == 'en'
        else
        'Сначала пункты (3-7) с таймкодами, затем 2-3 абзаца пересказа, каждый начинается с таймкода.\n\nРасшифровка:\n'
    )
    prompt = prompt_header + full_transcript_text

    await robust_edit_text(
        status_msg,
        'Generating summary…' if lang == 'en' else 'Генерирую аннотацию…',
        context, update, menu
    )

    try:
        response = await openai.ChatCompletion.acreate(
            model='gpt-3.5-turbo',
            messages=[
                {'role': 'system', 'content':
                    'You are an expert at summarizing video transcripts concisely and accurately.'},
                {'role': 'user', 'content': prompt},
            ],
            max_tokens=800,
            temperature=0.5,
        )
        summary_text = response.choices[0].message.content.strip()
        await robust_edit_text(
            status_msg, summary_text, context, update, menu, parse_mode='Markdown'
        )

    except openai.error.OpenAIError as e:
        logger.error(f"OpenAI API error: {e}")
        await robust_edit_text(
            status_msg,
            f"OpenAI error: {e}" if lang == 'en' else f"Ошибка OpenAI: {e}",
            context, update, menu
        )

# -----------------------------------------------------------------------------
# WEBHOOK ENTRYPOINT
# -----------------------------------------------------------------------------
if __name__ == '__main__':
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('language', language_cmd))
    application.add_handler(CommandHandler('help', help_cmd))
    application.add_handler(CallbackQueryHandler(language_button, pattern='^lang_'))

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    webhook_path = f"/{BOT_TOKEN.split(':')[-1]}"
    webhook_url  = APP_URL.rstrip('/') + webhook_path
    logger.info(f"Starting webhook: {webhook_url} on port {PORT}, path {webhook_path}")

    application.run_webhook(
        listen='0.0.0.0',
        port=PORT,
        url_path=webhook_path,
        webhook_url=webhook_url,
        drop_pending_updates=True,
    )
