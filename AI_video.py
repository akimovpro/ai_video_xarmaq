import os
import logging
import re
import httpx # Note: httpx is imported but not explicitly used in the provided snippet. Keeping it as is.
# import threading # Note: threading is imported but not explicitly used. Removed for clarity unless needed.
# import time # Note: time is imported but not explicitly used. Removed for clarity unless needed.
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
from pytube import YouTube # Ensure pytube is installed: pip install pytube

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
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# User language preferences
user_languages = {}

# Regex patterns for YouTube URLs
# Pattern for standard YouTube links to extract 11-character video ID
YOUTUBE_STD_REGEX = re.compile(
    r'(?:https?://)?(?:www\.)?'
    r'(?:youtube\.com/(?:watch\?v=|shorts/|live/|embed/|v/)|youtu\.be/)'
    r'([A-Za-z0-9_-]{11})'
)
# Pattern for googleusercontent.com/youtube.com/NUMERIC_ID links
YOUTUBE_GOOGLEUSERCONTENT_NUMERIC_REGEX = re.compile(
    r'(https?://(?:www\.)?googleusercontent\.com/youtube\.com/([0-9]+))'
)


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
    # Consider deleting the message with the language buttons if it's an inline keyboard from a previous message
    # await q.message.delete() # Uncomment if you want to delete the message that contained the inline keyboard

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

# Fetch transcript with pytube fallback
def fetch_transcript(video_id: str): # Expects 11-character video_id
    logger.info(f"Fetching transcript for 11-char video_id: {video_id}")
    # Try youtube_transcript_api first
    try:
        transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
        transcript = None
        # Try to find Russian or English transcript, prioritize manual, then generated
        for lang_code in ['ru', 'en']:
            try:
                transcript = transcript_list.find_manually_created_transcript([lang_code])
                logger.info(f"Found manual '{lang_code}' transcript for {video_id} via youtube_transcript_api.")
                break
            except:
                continue
        if not transcript:
            for lang_code in ['ru', 'en']:
                try:
                    transcript = transcript_list.find_generated_transcript([lang_code])
                    logger.info(f"Found generated '{lang_code}' transcript for {video_id} via youtube_transcript_api.")
                    break
                except:
                    continue
        
        if transcript:
            return transcript.fetch()
        else:
             logger.warning(f"No ru/en transcript found by youtube_transcript_api for {video_id}. Attempting pytube fallback.")

    except Exception as e:
        logger.warning(f'youtube_transcript_api error for video_id {video_id}: {e}')
        # Fall through to pytube

    # Fallback to pytube captions using the 11-character video_id
    try:
        logger.info(f"Attempting pytube fallback for video_id: {video_id}")
        # Construct standard URL for pytube using the 11-character ID
        standard_url = f'https://www.youtube.com/watch?v={video_id}'
        yt = YouTube(standard_url)
        
        cap = None
        # Try 'ru', then 'en', then 'a.ru' (auto ru), then 'a.en' (auto en)
        lang_prefs = ['ru', 'en', 'a.ru', 'a.en'] 
        pytube_captions = yt.captions
        
        for lang_code in lang_prefs:
            if lang_code in pytube_captions:
                cap = pytube_captions[lang_code]
                logger.info(f"Pytube found caption: {cap.code} for video {video_id} using URL {standard_url}")
                break
        
        if not cap and len(pytube_captions) > 0: # If no preferred found, take the first available one
            cap = pytube_captions[0]
            logger.info(f"Pytube: No preferred (ru/en) caption. Using first available: {cap.code} for video {video_id}")


        if not cap:
            logger.warning(f'Pytube: No captions found (even after checking all available) for video {video_id} using URL {standard_url}')
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
            logger.warning(f"Pytube: SRT parsing yielded no entries for {video_id} with caption {cap.code if cap else 'N/A'}")
            return None
            
        logger.info(f"Pytube successfully processed captions for {video_id} using {cap.code if cap else 'N/A'}")
        return entries
        
    except Exception as e:
        logger.error(f'Pytube fallback error for video_id {video_id}: {e}')
        return None

# Main message handler
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
    
    std_match = YOUTUBE_STD_REGEX.search(text_input)
    if std_match:
        video_id_11_char = std_match.group(1)
        logger.info(f"Extracted standard 11-char video ID: {video_id_11_char}")
    else:
        guc_match = YOUTUBE_GOOGLEUSERCONTENT_NUMERIC_REGEX.search(text_input)
        if guc_match:
            numeric_url = guc_match.group(1) # Full URL like https://youtu.be/LfHC7wblzR8?si=6x91L8zC-0MegGFL
            numeric_id_part = guc_match.group(2) # The '6'
            logger.info(f"Detected googleusercontent numeric URL: {numeric_url} (ID part: {numeric_id_part})")
            try:
                # Let Pytube resolve this URL to get the 11-character video_id
                status_message_resolve = await update.message.reply_text(
                    'Resolving special link format...' if lang == 'en' else 'Обработка специального формата ссылки...',
                    reply_markup=menu
                )
                yt_obj = YouTube(numeric_url)
                video_id_11_char = yt_obj.video_id
                
                if not (video_id_11_char and re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id_11_char)):
                    logger.warning(f"Pytube resolved {numeric_url} to '{video_id_11_char}', which is not a valid 11-char ID.")
                    video_id_11_char = None # Invalidate if not a proper 11-char ID
                else:
                    logger.info(f"Pytube resolved {numeric_url} to 11-char video ID: {video_id_11_char}")
                    await status_message_resolve.delete() # Clean up resolving message
            except Exception as e:
                logger.error(f"Failed to resolve numeric URL {numeric_url} with pytube: {e}")
                error_msg_resolve = 'Could not resolve this video link format. Pytube error.' if lang == 'en' else 'Не удалось обработать этот формат ссылки. Ошибка Pytube.'
                if status_message_resolve: # If message was sent
                   await status_message_resolve.edit_text(error_msg_resolve, reply_markup=menu)
                else: # Fallback if status message wasn't sent for some reason
                   await update.message.reply_text(error_msg_resolve, reply_markup=menu)
                return
        else:
            msg = 'Invalid YouTube URL. Please send a valid link.' if lang == 'en' else 'Недействительная ссылка YouTube. Пожалуйста, отправьте действительную ссылку.'
            await update.message.reply_text(msg, reply_markup=menu)
            return

    if not video_id_11_char:
        msg = 'Could not extract a valid video ID from the link provided.' if lang == 'en' else 'Не удалось извлечь действительный ID видео из предоставленной ссылки.'
        await update.message.reply_text(msg, reply_markup=menu)
        return
    
    # Now, `video_id_11_char` should hold the 11-character video ID
    vid = video_id_11_char 
    
    processing_msg = 'Processing the video... this might take a moment.' if lang == 'en' else 'Обрабатываю видео... это может занять некоторое время.'
    # If status_message_resolve was used and deleted, we send a new one.
    # Otherwise, we might want to edit an existing message if one was sent before ID resolution.
    # For simplicity here, we'll always send a new "Processing" message if we didn't already have one from resolution failure.
    status_message = await update.message.reply_text(processing_msg, reply_markup=menu)
    
    trans = fetch_transcript(vid) # `vid` is now the 11-character ID
    
    if not trans:
        msg = 'Sorry, I could not retrieve subtitles for this video. They might be unavailable or disabled.' if lang == 'en' else 'К сожалению, не удалось получить субтитры для этого видео. Возможно, они недоступны или отключены.'
        await status_message.edit_text(msg, reply_markup=menu)
        return

    parts = []
    for entry in trans:
        start_seconds = int(entry.get('start', 0)) # Use .get for safety
        text_content = entry.get('text', '')
        minutes = start_seconds // 60
        seconds = start_seconds % 60
        parts.append(f"[{minutes:02d}:{seconds:02d}] {text_content}")
    
    full_transcript_text = "\n".join(parts)
    
    max_chars_for_transcript = 10000 
    if len(full_transcript_text) > max_chars_for_transcript:
        full_transcript_text = full_transcript_text[:max_chars_for_transcript] + "\n[Transcript truncated due to length]"
        logger.info(f"Transcript for {vid} was truncated.")

    instr = (
        'You are a helpful assistant. Based on the following video transcript with timestamps, provide:'
        '\n1. A list of key bullet points (3-7 points) with their corresponding timestamps.'
        '\n2. A concise narrative summary of the video content in 2-3 paragraphs, starting each paragraph with a relevant timestamp or time range if applicable.'
        '\n\nTranscript:\n'
        if lang == 'en' else
        'Ты полезный ассистент. На основе следующей расшифровки видео с таймкодами, предоставь:'
        '\n1. Список ключевых моментов (3-7 пунктов) с соответствующими таймкодами.'
        '\n2. Краткий последовательный пересказ содержания видео в 2-3 абзацах, начиная каждый абзац с соответствующего таймкода или временного диапазона, если применимо.'
        '\n\nРасшифровка:\n'
    )
    prompt = instr + full_transcript_text

    try:
        await status_message.edit_text('Generating summary...' if lang == 'en' else 'Генерация аннотации...', reply_markup=menu)
        
        response = await openai.ChatCompletion.acreate(
            model='gpt-3.5-turbo',
            messages=[
                {'role': 'system', 'content': 'You are an expert at summarizing video transcripts concisely and accurately.'},
                {'role': 'user', 'content': prompt}
            ],
            max_tokens=800,
            temperature=0.5,
        )
        summary_text = response.choices[0].message.content.strip()
        await status_message.edit_text(summary_text, parse_mode='Markdown', reply_markup=menu)

    except openai.error.OpenAIError as e:
        logger.error(f"OpenAI API error: {e}")
        error_msg = f"Sorry, I encountered an error while generating the summary: {e}" if lang == 'en' else f"Извините, произошла ошибка при генерации аннотации: {e}"
        await status_message.edit_text(error_msg, reply_markup=menu)
    except Exception as e:
        logger.error(f"An unexpected error occurred in handle_message: {e}")
        error_msg = "An unexpected error occurred." if lang == 'en' else "Произошла непредвиденная ошибка."
        await status_message.edit_text(error_msg, reply_markup=menu)


# Webhook entry
if __name__=='__main__':
    application = Application.builder().token(BOT_TOKEN).build()
    
    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('language', language_cmd))
    application.add_handler(CommandHandler('help', help_cmd))
    
    application.add_handler(CallbackQueryHandler(language_button, pattern='^lang_'))
    
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    webhook_path = f"/{BOT_TOKEN.split(':')[-1]}" # Using a generic path part from token or a fixed string
    # Example: APP_URL = "https://your-app-name.onrender.com"
    # webhook_url should be "https://your-app-name.onrender.com/your_bot_path"
    # Ensure APP_URL does not end with a slash if webhook_path starts with one.
    webhook_url = APP_URL.rstrip('/') + webhook_path

    logger.info(f"Application built. Attempting to start webhook at {webhook_url} on port {PORT} with path {webhook_path}")
    
    application.run_webhook(
        listen='0.0.0.0',
        port=PORT,
        url_path=webhook_path, 
        webhook_url=webhook_url,
        drop_pending_updates=True
    )
