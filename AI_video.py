import os
import logging
import re
import httpx
import openai
import asyncio # Для запуска блокирующих операций в executor'е
import yt_dlp # Новая библиотека для субтитров

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Message
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
from telegram.error import BadRequest as TelegramBadRequest

# Загрузка переменных окружения (как и раньше)
BOT_TOKEN = os.getenv('BOT_TOKEN')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
APP_URL = os.getenv('APP_URL')
PORT = int(os.getenv('PORT', '443'))

if not BOT_TOKEN or not OPENAI_API_KEY or not APP_URL:
    raise RuntimeError('BOT_TOKEN, OPENAI_API_KEY, and APP_URL must be set')

# Инициализация OpenAI (как и раньше)
openai.api_key = OPENAI_API_KEY

# Логирование (как и раньше)
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Пользовательские языковые предпочтения (как и раньше)
user_languages = {}

# Регулярные выражения для URL (как и раньше)
YOUTUBE_STD_REGEX = re.compile(
    r'(?:https?://)?(?:www\.)?'
    r'(?:youtube\.com/(?:watch\?v=|shorts/|live/|embed/|v/)|youtu\.be/)'
    r'([A-Za-z0-9_-]{11})'
)
YOUTUBE_GOOGLEUSERCONTENT_NUMERIC_REGEX = re.compile(
    r'(https?://(?:www\.)?googleusercontent\.com/youtube\.com/([0-9]+))'
)

# --- Начало новой части: Парсер SRT и функция для yt-dlp ---

def parse_srt_content(srt_text: str, logger_obj=None) -> list | None:
    """Парсит содержимое SRT файла и возвращает список словарей с временем начала и текстом."""
    entries = []
    # Паттерн для SRT: номер, временные метки, текст (многострочный)
    # \s*? делает пробелы опциональными и нежадными
    pattern = re.compile(
        r"^\d+\s*?\n"
        r"(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})\s*?\n"
        r"(.+?)\s*?(\n\n|\Z)",
        re.S | re.M # re.S для точки, соответствующей новой строке в тексте, re.M для ^ в начале каждой строки
    )
    for match in pattern.finditer(srt_text):
        try:
            start_time_str = match.group(1) # HH:MM:SS,mmm
            raw_text_block = match.group(3)

            # Очистка текстового блока: удалить лишние пробелы и объединить строки через один пробел
            text_lines = [line.strip() for line in raw_text_block.strip().splitlines() if line.strip()]
            text_content = " ".join(text_lines)

            time_parts = start_time_str.split(',')
            h_m_s = time_parts[0].split(':')
            
            h = int(h_m_s[0])
            mn = int(h_m_s[1])
            s = int(h_m_s[2])
            # ms = int(time_parts[1]) # Миллисекунды пока не используются для `start`
            
            start_seconds = h * 3600 + mn * 60 + s
            
            if text_content: # Добавляем только если есть текст
                entries.append({'start': start_seconds, 'text': text_content})
        except Exception as e:
            if logger_obj:
                # Логируем только часть блока, чтобы не засорять логи слишком сильно
                logger_obj.error(f"Ошибка парсинга SRT блока: '{match.group(0)[:150].replace(chr(10), ' ')}...' -> {e}")
            continue # Пропускаем блок с ошибкой
    
    if not entries and srt_text: # Если парсинг ничего не дал, но текст был
         if logger_obj: logger_obj.warning("SRT контент был, но парсинг не дал записей. Проверьте формат SRT / паттерн.")
    return entries if entries else None


async def fetch_transcript_with_yt_dlp(video_url_or_id: str, target_langs=['ru', 'en'], logger_obj=logger) -> list | None:
    """
    Получает субтитры с помощью yt-dlp как Python модуль.
    video_url_or_id: Полный URL видео или 11-значный ID.
    target_langs: Список предпочитаемых языков ['ru', 'en'].
    logger_obj: Экземпляр логгера.
    """
    if logger_obj: logger_obj.info(f"yt-dlp: Запрос субтитров для '{video_url_or_id}' на языках: {target_langs}")

    ydl_opts = {
        'writesubtitles': True,        # Включить запись субтитров (если доступны)
        'writeautomaticsub': True,   # Включить запись автоматических субтитров
        'subtitleslangs': target_langs,  # Предпочитаемые языки ['ru', 'en', 'en-US', etc.]
        'subtitlesformat': 'srt',      # Желаемый формат субтитров
        'skip_download': True,         # Не скачивать само видео
        'quiet': True,                 # Меньше вывода от yt-dlp
        'noplaylist': True,            # Не обрабатывать плейлисты
        'noprogress': True,            # Не показывать прогресс-бар
        'logger': logger_obj,          # Использовать наш логгер
        'extract_flat': 'in_playlist', # Не извлекать информацию о каждом видео в плейлисте, если передан плейлист
        'ignoreerrors': True,          # Продолжать при ошибках с отдельными видео (если это плейлист)
    }

    try:
        # yt_dlp.YoutubeDL.extract_info() - это блокирующая операция.
        # Запускаем её в отдельном потоке, чтобы не блокировать asyncio event loop.
        loop = asyncio.get_running_loop()
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # Оборачиваем блокирующий вызов ydl.extract_info
            info_dict = await loop.run_in_executor(
                None,  # Использует ThreadPoolExecutor по умолчанию
                lambda: ydl.extract_info(video_url_or_id, download=False)
            )

        if not info_dict:
            if logger_obj: logger_obj.warning(f"yt-dlp: extract_info не вернул информацию для '{video_url_or_id}'")
            return None

        video_id_extracted = info_dict.get('id', 'N/A')
        if logger_obj: logger_obj.info(f"yt-dlp: Обработано видео: '{info_dict.get('title', 'N/A')}' (ID: {video_id_extracted})")

        chosen_sub_url = None
        chosen_lang_type = "" # "manual" or "auto"

        # Ищем субтитры в порядке предпочтения языков
        for lang_code in target_langs:
            # 1. Проверяем созданные вручную субтитры
            if lang_code in info_dict.get('subtitles', {}):
                for sub_info in info_dict['subtitles'][lang_code]:
                    if sub_info.get('ext') == 'srt' and sub_info.get('url'):
                        chosen_sub_url = sub_info['url']
                        chosen_lang_type = "manual"
                        if logger_obj: logger_obj.info(f"yt-dlp: Найдены ручные SRT для '{lang_code}'")
                        break
            if chosen_sub_url: break

            # 2. Проверяем автоматические субтитры, если ручные не найдены для этого языка
            if lang_code in info_dict.get('automatic_captions', {}):
                for sub_info in info_dict['automatic_captions'][lang_code]:
                    if sub_info.get('ext') == 'srt' and sub_info.get('url'):
                        chosen_sub_url = sub_info['url']
                        chosen_lang_type = "auto"
                        if logger_obj: logger_obj.info(f"yt-dlp: Найдены автоматические SRT для '{lang_code}'")
                        break
            if chosen_sub_url: break
        
        if not chosen_sub_url:
            if logger_obj: logger_obj.warning(f"yt-dlp: SRT субтитры на языках {target_langs} не найдены для '{video_url_or_id}'")
            return None

        if logger_obj: logger_obj.info(f"yt-dlp: Загрузка {chosen_lang_type} SRT субтитров с URL: {chosen_sub_url[:100]}...")
        
        async with httpx.AsyncClient(timeout=20.0) as client: # Увеличим таймаут для скачивания
            response = await client.get(chosen_sub_url)
            response.raise_for_status() # Вызовет исключение для HTTP ошибок 4xx/5xx
            srt_content = response.text
        
        if not srt_content:
            if logger_obj: logger_obj.warning(f"yt-dlp: Скачанный SRT контент пуст для '{video_url_or_id}'")
            return None

        return parse_srt_content(srt_content, logger_obj)

    except yt_dlp.utils.DownloadError as e:
        # Эта ошибка часто содержит полезную информацию, например, "subtitles not available"
        if logger_obj: logger_obj.error(f"yt-dlp DownloadError для '{video_url_or_id}': {str(e)}")
        return None
    except httpx.HTTPStatusError as e:
        if logger_obj: logger_obj.error(f"yt-dlp: Ошибка HTTP при скачивании субтитров для '{video_url_or_id}': {e}")
        return None
    except Exception as e:
        if logger_obj: logger_obj.error(f"yt-dlp: Общая ошибка при получении субтитров для '{video_url_or_id}': {type(e).__name__} - {e}")
        return None

# --- Конец новой части ---

# Вспомогательная функция для редактирования сообщений (без изменений)
async def robust_edit_text(
    message_to_edit: Message | None, new_text: str, context: ContextTypes.DEFAULT_TYPE,
    update_for_fallback: Update, reply_markup: InlineKeyboardMarkup | ReplyKeyboardMarkup | None,
    parse_mode: str | None = None
) -> Message | None:
    if message_to_edit:
        try:
            await message_to_edit.edit_text(new_text, reply_markup=reply_markup, parse_mode=parse_mode)
            return message_to_edit
        except TelegramBadRequest as e:
            if "Message is not modified" in str(e):
                logger.info(f"Message {message_to_edit.message_id} not modified.")
                return message_to_edit
            else: logger.warning(f"Failed to edit message {message_to_edit.message_id} (error: {e}). Sending new.")
        except Exception as e: logger.error(f"Unexpected error editing {message_to_edit.message_id}: {e}. Sending new.")
    else: logger.warning("robust_edit_text called with None message_to_edit. Sending new.")
    try:
        return await context.bot.send_message(
            chat_id=update_for_fallback.effective_chat.id, text=new_text,
            reply_markup=reply_markup, parse_mode=parse_mode
        )
    except Exception as e_send:
        logger.error(f"Failed to send fallback message: {e_send}")
        return None

# Клавиатуры и обработчики команд (без существенных изменений, кроме вызова fetch_transcript)
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

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        '🎉 *Welcome!* Select language / Выберите язык:',
        parse_mode='Markdown', reply_markup=get_lang_keyboard()
    )

async def language_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    lang = q.data.split('_')[1]; user_languages[q.from_user.id] = lang
    msg = '🌟 Language set to English!' if lang=='en' else '🌟 Язык установлен: Русский!'
    await q.message.reply_text(msg, parse_mode='Markdown', reply_markup=get_main_menu(lang))

async def language_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        '🌐 Select language / Выберите язык:', parse_mode='Markdown', reply_markup=get_lang_keyboard()
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = user_languages.get(update.effective_user.id, 'en')
    menu = get_main_menu(lang)
    text_en = '1️⃣ Send YouTube link (standard or googleusercontent.com/youtube.com/NUMERIC_ID format)\n2️⃣ Receive bullet points + narrative summary\n3️⃣ /language to change language'
    text_ru = '1️⃣ Отправьте ссылку YouTube (стандартного формата или googleusercontent.com/youtube.com/ЧИСЛОВОЙ_ID)\n2️⃣ Получите пункты + пересказ\n3️⃣ /language для смены языка'
    await update.message.reply_text(text_en if lang == 'en' else text_ru, parse_mode='Markdown', reply_markup=menu)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = user_languages.get(uid)
    text_input = update.message.text.strip()
    menu = get_main_menu(lang) if lang else None

    if not lang:
        await update.message.reply_text('Please select a language first using /start or /language.')
        return
    
    if text_input in ['📺 Summarize Video', '📺 Аннотировать видео']:
        msg = 'Please send me a YouTube video link to summarize:' if lang == 'en' else 'Пожалуйста, отправьте мне ссылку на YouTube видео для аннотации:'
        await update.message.reply_text(msg, reply_markup=menu)
        return
    if text_input in ['🌐 Change Language', '🌐 Сменить язык']:
        await language_cmd(update, context)
        return
    if text_input in ['❓ Help', '❓ Помощь']:
        await help_cmd(update, context)
        return

    video_url_or_id_for_yt_dlp = None # Это будет либо URL, либо 11-значный ID
    status_message_resolve = None 

    std_match = YOUTUBE_STD_REGEX.search(text_input)
    if std_match:
        video_url_or_id_for_yt_dlp = std_match.group(1) # Используем 11-значный ID
        logger.info(f"Extracted standard 11-char video ID: {video_url_or_id_for_yt_dlp}")
    else:
        guc_match = YOUTUBE_GOOGLEUSERCONTENT_NUMERIC_REGEX.search(text_input)
        if guc_match:
            numeric_url = guc_match.group(1)
            logger.info(f"Detected googleusercontent numeric URL: {numeric_url}")
            # yt-dlp может сам обработать этот URL, так что передаем его напрямую
            video_url_or_id_for_yt_dlp = numeric_url
            # Можно опционально попытаться извлечь 11-значный ID через pytube, если yt-dlp вдруг не справится
            # Но сейчас мы полагаемся на yt-dlp для обработки всех URL.
            # Сообщение "Resolving" теперь менее актуально, так как yt-dlp делает это внутренне.
        else:
            msg = 'Invalid YouTube URL.' if lang == 'en' else 'Недействительная ссылка YouTube.'
            await update.message.reply_text(msg, reply_markup=menu)
            return

    if not video_url_or_id_for_yt_dlp:
        msg = 'Could not extract a valid video ID/URL.' if lang == 'en' else 'Не удалось извлечь валидный ID/URL видео.'
        await update.message.reply_text(msg, reply_markup=menu)
        return
    
    processing_msg_text = 'Processing the video... this might take a moment.' if lang == 'en' else 'Обрабатываю видео... это может занять некоторое время.'
    status_message = await update.message.reply_text(processing_msg_text, reply_markup=menu)
    
    # Вызываем новую функцию с yt-dlp. Передаем logger.
    trans = await fetch_transcript_with_yt_dlp(video_url_or_id_for_yt_dlp, logger_obj=logger)
    
    if not trans:
        no_trans_msg = ('Sorry, I could not retrieve subtitles for this video with yt-dlp. They might be unavailable or disabled.'
                        if lang == 'en' else
                        'К сожалению, не удалось получить субтитры для этого видео с помощью yt-dlp. Возможно, они недоступны или отключены.')
        status_message = await robust_edit_text(status_message, no_trans_msg, context, update, menu)
        return

    parts = []
    for entry in trans:
        start_seconds = int(entry.get('start', 0))
        text_content = entry.get('text', '')
        minutes = start_seconds // 60
        seconds = start_seconds % 60
        parts.append(f"[{minutes:02d}:{seconds:02d}] {text_content}")
    full_transcript_text = "\n".join(parts)
    
    max_chars_for_transcript = 10000
    if len(full_transcript_text) > max_chars_for_transcript:
        full_transcript_text = full_transcript_text[:max_chars_for_transcript] + "\n[Transcript truncated...]"
        logger.info(f"Transcript for {video_url_or_id_for_yt_dlp} was truncated.")

    instr_en = ('List key bullet points (3-7) with timestamps, then a concise 2-3 paragraph summary starting each with timestamp.\n\nTranscript:\n')
    instr_ru = ('Сначала пункты (3-7) с таймкодами, затем 2-3 абзаца пересказа с таймкодами в начале каждого.\n\nРасшифровка:\n')
    prompt = (instr_en if lang == 'en' else instr_ru) + full_transcript_text

    gen_summary_msg = 'Generating summary...' if lang == 'en' else 'Генерация аннотации...'
    status_message = await robust_edit_text(status_message, gen_summary_msg, context, update, menu)

    try:
        response = await openai.ChatCompletion.acreate(
            model='gpt-3.5-turbo',
            messages=[
                {'role': 'system', 'content': 'You are an expert at summarizing video transcripts concisely and accurately.'},
                {'role': 'user', 'content': prompt}
            ],
            max_tokens=800, temperature=0.5,
        )
        summary_text = response.choices[0].message.content.strip()
        status_message = await robust_edit_text(status_message, summary_text, context, update, menu, parse_mode='Markdown')
    except openai.error.OpenAIError as e:
        logger.error(f"OpenAI API error: {e}")
        openai_err_msg = (f"OpenAI error: {e}" if lang == 'en' else f"Ошибка OpenAI: {e}")
        status_message = await robust_edit_text(status_message, openai_err_msg, context, update, menu)
    except Exception as e:
        logger.error(f"Unexpected error in handle_message (OpenAI part): {e}")
        unexpected_err_msg = "Unexpected error with OpenAI." if lang == 'en' else "Непредвиденная ошибка с OpenAI."
        status_message = await robust_edit_text(status_message, unexpected_err_msg, context, update, menu)

# Webhook entry (как и раньше)
if __name__=='__main__':
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler('start', start))
    # ... (остальные обработчики как в предыдущей версии) ...
    application.add_handler(CommandHandler('language', language_cmd))
    application.add_handler(CommandHandler('help', help_cmd))
    application.add_handler(CallbackQueryHandler(language_button, pattern='^lang_'))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    webhook_path = f"/{BOT_TOKEN.split(':')[-1]}" 
    webhook_url = APP_URL.rstrip('/') + webhook_path
    logger.info(f"Starting webhook: {webhook_url} on port {PORT}, path {webhook_path}")
    application.run_webhook(
        listen='0.0.0.0', port=PORT, url_path=webhook_path,
        webhook_url=webhook_url, drop_pending_updates=True
    )
