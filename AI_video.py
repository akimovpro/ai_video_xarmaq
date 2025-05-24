import os
import logging
import re
import httpx
import openai
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Message
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
from telegram.error import BadRequest as TelegramBadRequest # Импортируем для явного отлова

from youtube_transcript_api import YouTubeTranscriptApi
from pytube import YouTube

# Load environment variables
BOT_TOKEN = os.getenv('BOT_TOKEN')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
APP_URL = os.getenv('APP_URL')
PORT = int(os.getenv('PORT', '443'))

if not BOT_TOKEN or not OPENAI_API_KEY or not APP_URL:
    raise RuntimeError('BOT_TOKEN, OPENAI_API_KEY, and APP_URL must be set')

# Initialize OpenAI
openai.api_key = OPENAI_API_KEY

# Logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# User language preferences
user_languages = {}

YOUTUBE_STD_REGEX = re.compile(
    r'(?:https?://)?(?:www\.)?'
    r'(?:youtube\.com/(?:watch\?v=|shorts/|live/|embed/|v/)|youtu\.be/)'
    r'([A-Za-z0-9_-]{11})'
)
YOUTUBE_GOOGLEUSERCONTENT_NUMERIC_REGEX = re.compile(
    r'(https?://(?:www\.)?googleusercontent\.com/youtube\.com/([0-9]+))'
)

# Helper function for robust message editing
async def robust_edit_text(
    message_to_edit: Message | None,
    new_text: str,
    context: ContextTypes.DEFAULT_TYPE,
    update_for_fallback: Update, # Нужен для chat_id, если придется отправлять новое сообщение
    reply_markup: InlineKeyboardMarkup | ReplyKeyboardMarkup | None,
    parse_mode: str | None = None
) -> Message | None:
    """
    Tries to edit a message. If it fails (e.g., message too old),
    sends a new message instead. Returns the (potentially new) message object or None.
    """
    if message_to_edit:
        try:
            await message_to_edit.edit_text(new_text, reply_markup=reply_markup, parse_mode=parse_mode)
            return message_to_edit
        except TelegramBadRequest as e:
            if "Message is not modified" in str(e):
                logger.info(f"Message {message_to_edit.message_id} not modified, no need to edit.")
                return message_to_edit
            else:
                logger.warning(
                    f"Failed to edit message {message_to_edit.message_id} (error: {e}). Sending new message."
                )
        except Exception as e: # Catch other potential errors during edit
            logger.error(
                f"Unexpected error editing message {message_to_edit.message_id}: {e}. Sending new message."
            )
    else:
        logger.warning("robust_edit_text called with None message_to_edit. Sending new message.")

    # Fallback: send a new message
    try:
        new_msg = await context.bot.send_message(
            chat_id=update_for_fallback.effective_chat.id,
            text=new_text,
            reply_markup=reply_markup,
            parse_mode=parse_mode
        )
        return new_msg
    except Exception as e_send:
        logger.error(f"Failed to send fallback message: {e_send}")
        return None


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
    # Send as a new message, then delete the one with inline keyboard if desired
    await q.message.reply_text(msg, parse_mode='Markdown', reply_markup=get_main_menu(lang))
    # try:
    #     await q.message.delete() # Optional: delete the message with the lang buttons
    # except Exception as e:
    #     logger.warning(f"Could not delete language selection message: {e}")


async def language_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        '🌐 Select language / Выберите язык:', parse_mode='Markdown', reply_markup=get_lang_keyboard()
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = user_languages.get(uid, 'en')
    menu = get_main_menu(lang)
    if lang=='en':
        text = '1️⃣ Send YouTube link (standard or googleusercontent.com/youtube.com/NUMERIC_ID format)\n2️⃣ Receive bullet points + narrative summary\n3️⃣ /language to change language'
    else:
        text = '1️⃣ Отправьте ссылку YouTube (стандартного формата или googleusercontent.com/youtube.com/ЧИСЛОВОЙ_ID)\n2️⃣ Получите пункты + пересказ\n3️⃣ /language для смены языка'
    await update.message.reply_text(text, parse_mode='Markdown', reply_markup=menu)


def fetch_transcript(video_id: str): # Expects 11-character video_id
    logger.info(f"Fetching transcript for 11-char video_id: {video_id}")
    logger.info("Reminder: Ensure 'youtube-transcript-api' and 'pytube' are up-to-date ('pip install --upgrade youtube-transcript-api pytube')")
    try:
        transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
        transcript = None
        for lang_code in ['ru', 'en']:
            try:
                transcript = transcript_list.find_manually_created_transcript([lang_code])
                logger.info(f"Found manual '{lang_code}' transcript for {video_id} via youtube_transcript_api.")
                break
            except: continue
        if not transcript:
            for lang_code in ['ru', 'en']:
                try:
                    transcript = transcript_list.find_generated_transcript([lang_code])
                    logger.info(f"Found generated '{lang_code}' transcript for {video_id} via youtube_transcript_api.")
                    break
                except: continue
        if transcript:
            return transcript.fetch()
        else:
             logger.warning(f"No ru/en transcript found by youtube_transcript_api for {video_id}. Attempting pytube fallback.")
    except Exception as e:
        logger.warning(f'youtube_transcript_api error for video_id {video_id}: {e}')

    try:
        logger.info(f"Attempting pytube fallback for video_id: {video_id}")
        standard_url = f'https://www.youtube.com/watch?v={video_id}'
        yt = YouTube(standard_url)
        cap = None
        lang_prefs = ['ru', 'en', 'a.ru', 'a.en']
        pytube_captions = yt.captions
        for lang_code in lang_prefs:
            if lang_code in pytube_captions:
                cap = pytube_captions[lang_code]
                logger.info(f"Pytube found caption: {cap.code} for video {video_id}")
                break
        if not cap and len(pytube_captions) > 0:
            cap = pytube_captions[0]
            logger.info(f"Pytube: No preferred (ru/en) caption. Using first available: {cap.code} for video {video_id}")
        if not cap:
            logger.warning(f'Pytube: No captions found for video {video_id} using URL {standard_url}')
            return None
        srt = cap.generate_srt_captions()
        entries = []
        pattern = re.compile(r"\d+\n(\d{2}):(\d{2}):(\d{2}),\d{3} --> \d{2}:\d{2}:\d{2},\d{3}\n(.*?)(?:\n\n|\Z)", re.S)
        for match in pattern.finditer(srt):
            h, mn, s = int(match.group(1)), int(match.group(2)), int(match.group(3))
            start_time = h * 3600 + mn * 60 + s
            text_content = match.group(4).replace('\n', ' ').strip()
            entries.append({'start': start_time, 'text': text_content})
        if not entries:
            logger.warning(f"Pytube: SRT parsing yielded no entries for {video_id}")
            return None
        logger.info(f"Pytube successfully processed captions for {video_id}")
        return entries
    except Exception as e:
        logger.error(f'Pytube fallback error for video_id {video_id}: {e}') # This was the "HTTP Error 400"
        return None

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

    video_id_11_char = None
    status_message_resolve = None # To keep track of the "resolving" message

    std_match = YOUTUBE_STD_REGEX.search(text_input)
    if std_match:
        video_id_11_char = std_match.group(1)
        logger.info(f"Extracted standard 11-char video ID: {video_id_11_char}")
    else:
        guc_match = YOUTUBE_GOOGLEUSERCONTENT_NUMERIC_REGEX.search(text_input)
        if guc_match:
            numeric_url = guc_match.group(1)
            numeric_id_part = guc_match.group(2)
            logger.info(f"Detected googleusercontent numeric URL: {numeric_url} (ID part: {numeric_id_part})")
            resolve_msg_text = 'Resolving special link format...' if lang == 'en' else 'Обработка специального формата ссылки...'
            status_message_resolve = await update.message.reply_text(resolve_msg_text, reply_markup=menu)
            try:
                yt_obj = YouTube(numeric_url)
                video_id_11_char = yt_obj.video_id
                if not (video_id_11_char and re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id_11_char)):
                    logger.warning(f"Pytube resolved {numeric_url} to '{video_id_11_char}', not a valid 11-char ID.")
                    video_id_11_char = None
                else:
                    logger.info(f"Pytube resolved {numeric_url} to 11-char video ID: {video_id_11_char}")
                    if status_message_resolve: # Delete "resolving" message on success
                        try: await status_message_resolve.delete()
                        except Exception: pass # Ignore if already deleted or other issue
                        status_message_resolve = None # Clear it
            except Exception as e:
                logger.error(f"Failed to resolve numeric URL {numeric_url} with pytube: {e}")
                error_msg_resolve = 'Could not resolve this video link format. Pytube error.' if lang == 'en' else 'Не удалось обработать этот формат ссылки. Ошибка Pytube.'
                status_message_resolve = await robust_edit_text(status_message_resolve, error_msg_resolve, context, update, menu)
                return # Stop processing if resolution failed
        else:
            msg = 'Invalid YouTube URL. Please send a valid link.' if lang == 'en' else 'Недействительная ссылка YouTube. Пожалуйста, отправьте действительную ссылку.'
            await update.message.reply_text(msg, reply_markup=menu)
            return

    if not video_id_11_char:
        # If status_message_resolve still exists here, it means resolution failed and message was already updated by robust_edit_text.
        # If it's None, and no video_id, means it wasn't a numeric URL either.
        if not status_message_resolve : # Only send if no previous error message was shown
            msg = 'Could not extract a valid video ID from the link provided.' if lang == 'en' else 'Не удалось извлечь действительный ID видео из предоставленной ссылки.'
            await update.message.reply_text(msg, reply_markup=menu)
        return
    
    vid = video_id_11_char
    processing_msg_text = 'Processing the video... this might take a moment.' if lang == 'en' else 'Обрабатываю видео... это может занять некоторое время.'
    status_message = await update.message.reply_text(processing_msg_text, reply_markup=menu)
    
    trans = fetch_transcript(vid)
    
    if not trans:
        no_trans_msg = ('Sorry, I could not retrieve subtitles for this video. They might be unavailable or disabled.'
                        if lang == 'en' else
                        'К сожалению, не удалось получить субтитры для этого видео. Возможно, они недоступны или отключены.')
        status_message = await robust_edit_text(status_message, no_trans_msg, context, update, menu)
        return # Important to return here

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
        full_transcript_text = full_transcript_text[:max_chars_for_transcript] + "\n[Transcript truncated due to length]"
        logger.info(f"Transcript for {vid} was truncated.")

    instr_en = ('You are a helpful assistant. Based on the following video transcript with timestamps, provide:'
                '\n1. A list of key bullet points (3-7 points) with their corresponding timestamps.'
                '\n2. A concise narrative summary of the video content in 2-3 paragraphs, starting each paragraph with a relevant timestamp or time range if applicable.'
                '\n\nTranscript:\n')
    instr_ru = ('Ты полезный ассистент. На основе следующей расшифровки видео с таймкодами, предоставь:'
                '\n1. Список ключевых моментов (3-7 пунктов) с соответствующими таймкодами.'
                '\n2. Краткий последовательный пересказ содержания видео в 2-3 абзацах, начиная каждый абзац с соответствующего таймкода или временного диапазона, если применимо.'
                '\n\nРасшифровка:\n')
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
        openai_err_msg = (f"Sorry, I encountered an error while generating the summary: {e}"
                          if lang == 'en' else
                          f"Извините, произошла ошибка при генерации аннотации: {e}")
        status_message = await robust_edit_text(status_message, openai_err_msg, context, update, menu)
    except Exception as e:
        logger.error(f"An unexpected error occurred in handle_message: {e}")
        unexpected_err_msg = "An unexpected error occurred." if lang == 'en' else "Произошла непредвиденная ошибка."
        status_message = await robust_edit_text(status_message, unexpected_err_msg, context, update, menu)

# Webhook entry
if __name__=='__main__':
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('language', language_cmd))
    application.add_handler(CommandHandler('help', help_cmd))
    application.add_handler(CallbackQueryHandler(language_button, pattern='^lang_'))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    webhook_path = f"/{BOT_TOKEN.split(':')[-1]}" # Or a fixed path like "/webhook"
    webhook_url = APP_URL.rstrip('/') + webhook_path
    logger.info(f"Attempting to start webhook at {webhook_url} on port {PORT} with path {webhook_path}")
    application.run_webhook(
        listen='0.0.0.0', port=PORT, url_path=webhook_path,
        webhook_url=webhook_url, drop_pending_updates=True
    )
