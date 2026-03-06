"""
AskMovies Poster Bot
====================
Monitors log channel → posts immediately to public channel
→ edits post when more qualities arrive for same movie.

Environment Variables:
  BOT_TOKEN          = Telegram bot token
  TMDB_API_KEY       = TMDB API key
  LOG_CHANNEL_ID     = Log channel ID
  PUBLIC_CHANNEL_ID  = Public destination channel ID
  FILESTORE_BOT      = File store bot username (without @)
  WEBHOOK_URL        = Koyeb app URL
"""

import os, re, base64, logging, asyncio, json
from datetime import datetime, timezone
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
from telegram.constants import ParseMode

BOT_TOKEN         = os.environ["BOT_TOKEN"]
TMDB_API_KEY      = os.environ["TMDB_API_KEY"]
LOG_CHANNEL_ID    = str(os.environ["LOG_CHANNEL_ID"])
PUBLIC_CHANNEL_ID = str(os.environ["PUBLIC_CHANNEL_ID"])
FILESTORE_BOT     = os.environ["FILESTORE_BOT"].lstrip("@")
WEBHOOK_URL       = os.environ["WEBHOOK_URL"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# posted[movie_key] = {title, year, languages, quality_label, is_series,
#                      files:[{quality, link}], message_id, poster_url}
posted = {}
posted_lock = asyncio.Lock()

# ── TMDB ─────────────────────────────────────────────────────
def fetch_tmdb_poster(title: str, year=None) -> str:
    try:
        params = {"api_key": TMDB_API_KEY, "query": title, "language": "en-US"}
        if year:
            params["year"] = year
        for endpoint in ["movie", "tv"]:
            r = requests.get(f"https://api.themoviedb.org/3/search/{endpoint}", params=params, timeout=10)
            results = r.json().get("results", [])
            if results and results[0].get("poster_path"):
                return f"https://image.tmdb.org/t/p/w500{results[0]['poster_path']}"
    except Exception as e:
        log.error(f"TMDB error: {e}")
    return None

# ── FileStore link ────────────────────────────────────────────
def make_filestore_link(file_id: str) -> str:
    encoded = base64.b64encode(file_id.encode()).decode()
    return f"https://t.me/{FILESTORE_BOT}?start=fs_{encoded}"

# ── Parse log message ─────────────────────────────────────────
def parse_log_message(text: str) -> dict:
    if not text:
        return None

    lines = text.strip().splitlines()
    result = {}

    # Get filename from first line - strip [ASK] emoji prefixes
    first_line = lines[0]
    first_line = re.sub(r'\[.*?\]', '', first_line).strip()
    first_line = re.sub(r'[^\x00-\x7F\u0080-\u024F\s\(\)\[\]\-\_\.]+', '', first_line).strip()
    filename = first_line

    # Extract year
    year_match = re.search(r'\((\d{4})\)|\b(20\d{2})\b', filename)
    year = int(year_match.group(1) or year_match.group(2)) if year_match else None

    # Extract title
    if year_match:
        title_raw = filename[:year_match.start()].strip()
    else:
        title_raw = re.split(r'\b(WEB-DL|HDRip|BluRay|WEBRip|HDCAM|480p|720p|1080p|4K|HQ)\b', filename, flags=re.IGNORECASE)[0]

    # Clean title
    title = re.sub(r'[_\-]+', ' ', title_raw).strip()
    title = re.sub(r'\s+', ' ', title).strip()
    # Remove file extension
    title = re.sub(r'\.(mkv|mp4|avi|mov)$', '', title, flags=re.IGNORECASE).strip()
    # Remove S01E01 patterns
    title = re.sub(r'\s*S\d{1,2}E?\d*\s*', ' ', title, flags=re.IGNORECASE).strip()
    title = re.sub(r'\s+', ' ', title).strip()

    if not title or len(title) < 2:
        return None

    result["title"] = title
    result["year"] = year
    result["filename"] = filename

    # Quality from lines
    quality = ""
    for line in lines:
        qm = re.search(r'Quality\s*:\s*#?(\w+)', line, re.IGNORECASE)
        if qm:
            quality = qm.group(1).replace('#', '')
            break
    if not quality:
        qm = re.search(r'\b(480p|720p|1080p|4K|2160p|240p|360p)\b', filename, re.IGNORECASE)
        if qm:
            quality = qm.group(1)
    result["quality"] = quality or "HD"

    # Quality label (WEB-DL, HDRip etc)
    qlm = re.search(r'\b(WEB-DL|HDRip|BluRay|WEBRip|HDCAM|HQ HDRip|TRUE WEB-DL|HQ)\b', filename, re.IGNORECASE)
    result["quality_label"] = qlm.group(1).upper() if qlm else "WEB-DL"

    # Languages
    languages = []
    for line in lines:
        if re.search(r'lang', line, re.IGNORECASE):
            lang_part = re.sub(r'[Ll]ang[a-z]*\s*:', '', line).strip()
            languages = [l.strip() for l in re.split(r'[,+]', lang_part) if l.strip()]
            break
    if not languages:
        lang_map = {'tam': 'Tamil', 'tel': 'Telugu', 'hin': 'Hindi',
                    'eng': 'English', 'mal': 'Malayalam', 'kan': 'Kannada'}
        for abbr, lang in lang_map.items():
            if abbr in filename.lower():
                languages.append(lang)
    result["languages"] = languages

    # Series detection
    result["is_series"] = bool(re.search(r'S\d{1,2}\s*EP?\d+', filename, re.IGNORECASE))

    # Extract file ID from workers.dev URL
    workers_match = re.search(r'/watch/(\d+)\?', text)
    if workers_match:
        result["file_id"] = workers_match.group(1)
        result["link"] = make_filestore_link(workers_match.group(1))
        log.info(f"🔗 File ID {workers_match.group(1)} → {result['link']}")

    return result

# ── Build caption ─────────────────────────────────────────────
def build_caption(data: dict) -> str:
    title         = data["title"]
    year          = data.get("year", "")
    languages     = data.get("languages", [])
    files         = data.get("files", [])
    is_series     = data.get("is_series", False)
    quality_label = data.get("quality_label", "WEB-DL")

    audio_str = " + ".join(languages) if languages else "Tamil"

    quality_order = {"240p":1,"360p":2,"480p":3,"720p":4,"1080p":5,"4K":6,"2160p":7}
    files_sorted = sorted(files, key=lambda x: quality_order.get(x.get("quality",""), 99))

    file_lines = ""
    for f in files_sorted:
        q    = f.get("quality", "HD")
        link = f.get("link", f"https://t.me/{FILESTORE_BOT}")
        file_lines += f'\n♨️ <a href="{link}">{title} ({year}) - {q}</a>'

    batch_link = files_sorted[-1].get("link", f"https://t.me/{FILESTORE_BOT}") if files_sorted else f"https://t.me/{FILESTORE_BOT}"

    season_line = ""
    if is_series:
        sm = re.search(r'S(\d{1,2})', data.get("filename", ""), re.IGNORECASE)
        if sm:
            season_line = f"\n💫 <b>Season:</b> {int(sm.group(1))}"

    return (
        f"<b>AskMovies</b>\n"
        f"🎬 <b>Title:</b> {title}\n"
        f"📅 <b>Year :</b> {year}"
        f"{season_line}\n"
        f"🎞 <b>Quality:</b> {quality_label}\n"
        f"🎧 <b>Audio:</b> {audio_str}\n\n"
        f"🔺<b>Telegram File</b>🔻"
        f"{file_lines}\n\n"
        f'📦 <b>Get all files in one link:</b> <a href="{batch_link}">Click Here</a>\n\n'
        f"Note 💢: If the link is not working, copy it and paste it into your browser.\n\n"
        f"❤️Join » @{FILESTORE_BOT}"
    )

# ── Handler ───────────────────────────────────────────────────
async def handle_log_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.channel_post or update.message
    if not msg or str(msg.chat.id) != LOG_CHANNEL_ID:
        return

    text = msg.text or msg.caption or ""
    if not text.strip():
        return

    log.info(f"📥 {text[:150]}")

    parsed = parse_log_message(text)
    if not parsed:
        return

    title     = parsed["title"]
    year      = parsed.get("year")
    movie_key = f"{title}_{year}".lower().replace(" ", "_")
    file_entry = None

    if parsed.get("link"):
        file_entry = {
            "quality": parsed["quality"],
            "link":    parsed["link"]
        }

    async with posted_lock:
        if movie_key in posted:
            # Movie already posted — edit existing message
            if file_entry:
                posted[movie_key]["files"].append(file_entry)
            data = posted[movie_key]
            caption = build_caption(data)
            try:
                await context.bot.edit_message_caption(
                    chat_id=PUBLIC_CHANNEL_ID,
                    message_id=data["message_id"],
                    caption=caption,
                    parse_mode=ParseMode.HTML
                )
                log.info(f"✏️ Edited post for: {title} — added {parsed['quality']}")
            except Exception as e:
                log.error(f"Edit failed: {e}")
        else:
            # New movie — post immediately
            files = [file_entry] if file_entry else []
            poster_url = fetch_tmdb_poster(title, year)

            data = {
                "title":         title,
                "year":          year,
                "languages":     parsed.get("languages", []),
                "quality_label": parsed.get("quality_label", "WEB-DL"),
                "is_series":     parsed.get("is_series", False),
                "filename":      parsed.get("filename", ""),
                "files":         files,
                "poster_url":    poster_url,
                "message_id":    None
            }

            caption = build_caption(data)
            try:
                if poster_url:
                    sent = await context.bot.send_photo(
                        chat_id=PUBLIC_CHANNEL_ID,
                        photo=poster_url,
                        caption=caption,
                        parse_mode=ParseMode.HTML
                    )
                else:
                    sent = await context.bot.send_message(
                        chat_id=PUBLIC_CHANNEL_ID,
                        text=caption,
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True
                    )
                data["message_id"] = sent.message_id
                posted[movie_key] = data
                log.info(f"✅ Posted: {title} ({year}) | msg_id={sent.message_id}")
            except Exception as e:
                log.error(f"❌ Post failed: {e}")

# ── Main ──────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    webhook_path = f"/webhook/{BOT_TOKEN}"
    full_webhook_url = f"{WEBHOOK_URL.rstrip('/')}{webhook_path}"

    log.info("🤖 AskMovies Poster Bot starting (webhook mode)")
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.ALL, handle_log_message))
    app.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=webhook_path,
        webhook_url=full_webhook_url
  )
  
