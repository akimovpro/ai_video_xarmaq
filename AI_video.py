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
PORT = int(os.getenv("PORT", "443"))

if not BOT_TOKEN or not OPENAI_API_KEY or not APP_URL:
    raise RuntimeError("BOT_TOKEN, OPENAI_API_KEY, and APP_URL must be set")

openai.api_key = OPENAI_API_KEY

# -----------------------------------------------------------------------------
# SOCKS5 PROXY (yt‑dlp only)
# -----------------------------------------------------------------------------
YTDLP_PROXY_USER = os.getenv("YTDLP_PROXY_USER")
YTDLP_PROXY_PASS = os.getenv("YTDLP_PROXY_PASS")
YTDLP_PROXY_HOST = "gate.decodo.com"
YTDLP_PROXY_PORT = 7000

if not YTDLP_PROXY_USER or not YTDLP_PROXY_PASS:
    raise RuntimeError("YTDLP_PROXY_USER and YTDLP_PROXY_PASS must be set")

YTDLP_PROXY_URL = (
    f"socks5h://{YTDLP_PROXY_USER}:{YTDLP_PROXY_PASS}"
    f"@{YTDLP_PROXY_HOST}:{YTDLP_PROXY_PORT}"
)

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
    # Дополнительные строки для чанкинга (если будете использовать в будущем)
    "summarizing_long_video": {"en": "📝 Summarizing long video, this may take a while...", "ru": "📝 Аннотируем длинное видео, это может занять некоторое время..."},
    "summarizing_chunk": {"en": "📝 Summarizing part", "ru": "📝 Аннотируем часть"},
    "creating_final_summary": {"en": "📝 Creating final summary...", "ru": "📝 Создаем итоговую аннотацию..."},
    "error_summarizing_chunk": {"en": "Error summarizing part", "ru": "Ошибка при аннотации части"},
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
        start_ts_str = m.group(1).split(",")[0]
        h, mi, s = map(int, start_ts_str.split(":"))
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
            ts_str = lines[i].split("-->")[0].strip()
            m = VTT_TS_RE.search(ts_str)
            i += 1
            body_lines = []
            while i < len(lines) and lines[i].strip():
                body_lines.append(lines[i].strip())
                i += 1
            if m and body_lines:
                h, mi, s = int(m.group("h")), int(m.group("m")), int(m.group("s"))
                entries.append({"start": _ts2sec(h, mi, s), "text": " ".join(body_lines)})
        i += 1
    return entries


def parse_captions(text: str, ext: str) -> list | None:
    return parse_srt(text) if ext == "srt" else parse_vtt(text) if ext == "vtt" else None


# -----------------------------------------------------------------------------
# FETCH CAPTIONS WITH MINIMUM TRAFFIC
# -----------------------------------------------------------------------------
async def fetch_transcript(video_id_or_url: str, langs: list[str] | None = None) -> list | None:
    langs = langs or ["ru", "en"]
    video_id_match = YOUTUBE_STD_REGEX.search(video_id_or_url)
    video_id = video_id_match.group(1) if video_id_match else video_id_or_url

    loop = asyncio.get_running_loop()
    try:
        transcript_data = await loop.run_in_executor(
            None, lambda: YouTubeTranscriptApi.get_transcript(video_id, languages=langs)
        )
        return [
            {"start": int(float(it["start"])), "text": it["text"]} # Сохраняем исходный текст с \n
            for it in transcript_data
            if it.get("text")
        ]
    except (TranscriptsDisabled, NoTranscriptFound, Exception) as e:  # noqa: BLE001
        logger.info("Transcript API failed (%s), falling back to yt_dlp", e)

    ydl_opts = {
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": langs,
        "subtitlesformat": "best",
        "skip_download": True,
        "quiet": True,
        "proxy": YTDLP_PROXY_URL,
        "logger": logger,
        "extract_flat": "in_playlist",
        "cachedir": False,
        "nocheckcertificate": True,
        "extractor_args": {"youtube": {"skip": ["dash"]}},
    }

    def _pick(pool):
        for lang_code in langs:
            for ext in ("srt", "vtt"):
                for it in pool.get(lang_code, []):
                    if it.get("ext") == ext and it.get("url"):
                        return it["url"], ext
        return None, None

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = await loop.run_in_executor(None, lambda: ydl.extract_info(video_id_or_url, download=False))
    if not info:
        return None
    
    subtitles_info = info.get("subtitles", {})
    auto_captions_info = info.get("automatic_captions", {})

    # Объединяем словари субтитров, чтобы поиск был по всем доступным
    # (на случай если yt-dlp вернул их в разных структурах для разных языков)
    # Приоритет ручным субтитрам, если язык совпадает
    merged_subs = {}
    for lang_code in langs:
        if lang_code in subtitles_info:
             merged_subs.setdefault(lang_code, []).extend(subtitles_info[lang_code])
        if lang_code in auto_captions_info:
             merged_subs.setdefault(lang_code, []).extend(auto_captions_info[lang_code])
    
    # Если для предпочтительных языков ничего нет, смотрим все что есть
    if not any(lang_code in merged_subs for lang_code in langs):
        for lang_code in subtitles_info: # все доступные ручные
            merged_subs.setdefault(lang_code, []).extend(subtitles_info[lang_code])
        for lang_code in auto_captions_info: # все доступные автоматические
             merged_subs.setdefault(lang_code, []).extend(auto_captions_info[lang_code])


    url, ext = _pick(merged_subs) # Используем _pick на объединенном словаре
    
    if not url:
        return None

    async with httpx.AsyncClient(timeout=30.0, headers={"Accept-Encoding": "gzip"}) as client:
        r = await client.get(url)
        r.raise_for_status()
    return parse_captions(r.text, ext)


# -----------------------------------------------------------------------------
# HELPERS
# -----------------------------------------------------------------------------
async def robust_edit(msg: Message | None, text: str, ctx, upd, kb, md: str | None = None):
    if msg:
        try:
            return await msg.edit_text(text, reply_markup=kb, parse_mode=md)
        except TelegramBadRequest:
            pass  # Если не удалось отредактировать, отправим новое сообщение
    return await ctx.bot.send_message(upd.effective_chat.id, text, reply_markup=kb, parse_mode=md)


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

# Новая вспомогательная функция для форматирования времени
def format_timestamp_hms(seconds: int) -> str:
    """Форматирует секунды в строку HH:MM:SS или MM:SS."""
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    else:
        return f"{m:02d}:{s:02d}"

# -----------------------------------------------------------------------------
# COMMANDS
# -----------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        tr("start_choose_language", "en"),  # Always show initial prompt in both for clarity
        parse_mode="Markdown",
        reply_markup=lang_kb(),
    )


async def language_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    lang = q.data.split("_")[1]
    user_languages[q.from_user.id] = lang
    await q.edit_message_text(
        tr("language_set", lang), 
        reply_markup=None # Убираем инлайн клавиатуру после выбора
    )
    # Отправляем новое сообщение с главным меню на выбранном языке
    await context.bot.send_message(
        chat_id=q.message.chat_id,
        text=f"{tr('language_set', lang)}\n{tr('prompt_send_link', lang)}", # Добавим сразу просьбу прислать ссылку
        reply_markup=main_menu(lang)
    )


async def language_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = user_languages.get(update.effective_user.id, "en")
    await update.message.reply_text(tr("select_language", lang), reply_markup=lang_kb())


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = user_languages.get(update.effective_user.id, "en")
    await update.message.reply_text(tr("help_text", lang), reply_markup=main_menu(lang))


# -----------------------------------------------------------------------------
# MESSAGE HANDLER
# -----------------------------------------------------------------------------
async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = user_languages.get(uid)
    if not lang:
        await update.message.reply_text(tr("select_language", "en"), reply_markup=lang_kb())
        return

    text = update.message.text.strip()
    kb = main_menu(lang)

    if text == MENU_ITEMS["summarize"][lang]:
        await update.message.reply_text(tr("prompt_send_link", lang), reply_markup=kb)
        return
    if text == MENU_ITEMS["change_lang"][lang]:
        await language_cmd(update, context)
        return
    if text == MENU_ITEMS["help"][lang]:
        await help_cmd(update, context)
        return

    vid = None
    m = YOUTUBE_STD_REGEX.search(text)
    if m:
        vid = m.group(1)
    else:
        m = YOUTUBE_GOOGLEUSERCONTENT_NUMERIC_REGEX.search(text)
        if m:
            vid = m.group(1)

    if not vid:
        await update.message.reply_text(tr("invalid_url", lang), reply_markup=kb)
        return

    status_msg = await update.message.reply_text(tr("fetching_captions", lang), reply_markup=kb)
    captions = await fetch_transcript(vid, langs=[lang, "en"]) # Запрашиваем на языке пользователя и английском
    
    if not captions:
        await robust_edit(status_msg, tr("subtitles_not_found", lang), context, update, kb)
        return

    # --- Новая логика формирования транскрипта с редкими метками ---
    TIME_INTERVAL_SECONDS = 60  # Ставить метку примерно каждые 60 секунд
    processed_transcript_parts = []
    current_block_texts = []
    current_block_start_time_sec = 0 
    # Инициализируем так, чтобы первая же запись вызвала создание нового блока, если интервал > 0
    last_timestamped_block_start_time_sec = -TIME_INTERVAL_SECONDS -1 

    if captions: # Убедимся, что субтитры есть
        # Устанавливаем время начала первого блока из первого субтитра
        current_block_start_time_sec = captions[0]['start'] 
    
        for caption_entry in captions:
            entry_start_time = caption_entry['start']
            # Очищаем текст от лишних пробелов и заменяем переносы строк внутри на пробелы
            entry_text = " ".join(caption_entry['text'].strip().splitlines())

            if not entry_text: # Пропускаем пустые строки после очистки
                continue
            
            # Условие для новой метки: 
            # 1. Прошло достаточно времени с момента установки последней метки ИЛИ
            # 2. Это самый первый текстовый блок, который мы добавляем (processed_transcript_parts еще пуст)
            if (entry_start_time >= last_timestamped_block_start_time_sec + TIME_INTERVAL_SECONDS) or \
               not processed_transcript_parts :
                
                if current_block_texts: # Если есть накопленный текст, завершаем предыдущий блок
                    block_text_content = " ".join(current_block_texts)
                    processed_transcript_parts.append(f"[{format_timestamp_hms(current_block_start_time_sec)}] {block_text_content}")
                
                # Начинаем новый блок
                current_block_texts = [entry_text]
                current_block_start_time_sec = entry_start_time 
                last_timestamped_block_start_time_sec = entry_start_time # Запоминаем время, когда была установлена метка для этого блока
            else:
                # Продолжаем накапливать текст для текущего блока
                current_block_texts.append(entry_text)

        # Добавляем последний накопленный блок, если он остался
        if current_block_texts:
            block_text_content = " ".join(current_block_texts)
            processed_transcript_parts.append(f"[{format_timestamp_hms(current_block_start_time_sec)}] {block_text_content}")

    transcript = "\n".join(processed_transcript_parts)
    if not transcript and captions: # Если субтитры были, но все текстовые строки оказались пустыми
        transcript = "[Транскрипт не содержит текстового содержимого]" if lang == "ru" else "[Transcript contains no textual content]"
    elif not captions: # Этот случай уже обработан выше, но для полноты
        transcript = "[Субтитры не найдены]" if lang == "ru" else "[Subtitles not found]"
    # --- Конец новой логики ---

    if not transcript.strip() or transcript.startswith("["): # Проверка, что транскрипт не пустой и не сообщение об ошибке
        # Сообщение об отсутствии субтитров или текста уже было отправлено выше или будет заменено
        # Если captions были, но transcript пуст (например, все строки были пробелами)
        if captions and not transcript.strip():
             await robust_edit(status_msg, tr("subtitles_not_found", lang), context, update, kb) # Можно уточнить ошибку
        # Если captions не было, то subtitles_not_found уже было отправлено.
        return


    if len(transcript) > 100000:
        transcript = transcript[:100000] + "\n[truncated]"

    instr = (
        "List 5-10 bullet points about the main things (with timestamps) then a 2-3 paragraph summary."
        if lang == "en"
        else "Сначала напиши 5-10 пунктов с основными мыслями с таймкодами, затем 2-3 абзаца пересказа."
    )
    prompt = f"{instr}\n\nTranscript:\n{transcript}"

    await robust_edit(status_msg, tr("summarizing", lang), context, update, kb)
    try:
        rsp = await openai.ChatCompletion.acreate(
            model="gpt-4o", # Рекомендую использовать более новую модель, если возможно, например gpt-4o или gpt-4-turbo
            messages=[
                {
                    "role": "system",
                    "content": "You are best in the world video summarizer. Preserve maximum details. Timestamps in your summary should correspond to the timestamps provided in the transcript.",
                },
                {"role": "user", "content": prompt},
            ],
            max_tokens=1000, # Можно увеличить, если ожидаются длинные аннотации
            temperature=0.5,
        )
        summ = rsp.choices[0].message.content.strip()
        # Проверка на слишком длинный ответ для Telegram (макс. 4096 символов)
        if len(summ) > 4096:
            await robust_edit(status_msg, summ[:4090] + "\n[...]", context, update, kb, md="Markdown") # Обрезаем и отправляем
            # Можно добавить логику отправки остальной части в новом сообщении, если это необходимо
        else:
            await robust_edit(status_msg, summ, context, update, kb, md="Markdown")

    except Exception as e:  # noqa: BLE001
        logger.error(f"OpenAI API error: {e}", exc_info=True)
        await robust_edit(status_msg, f"{tr('openai_error', lang)} {type(e).__name__}: {e}", context, update, kb)


# -----------------------------------------------------------------------------
# ENTRYPOINT
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("language", language_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CallbackQueryHandler(language_button, pattern="^lang_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))

    # Для локального тестирования можно закомментировать вебхук и использовать app.run_polling()
    # logger.info("Starting polling...")
    # app.run_polling(drop_pending_updates=True)

    # Настройки для вебхука (если разворачиваете на сервере)
    webhook_path = f"/{BOT_TOKEN.split(':')[-1]}" # Более безопасный способ получить часть токена для пути
    webhook_url = APP_URL.rstrip("/") + webhook_path
    logger.info("Starting webhook at %s on port %d", webhook_url, PORT)

    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=webhook_path,
        webhook_url=webhook_url,
        drop_pending_updates=True,
    )
