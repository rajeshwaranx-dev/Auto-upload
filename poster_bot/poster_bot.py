"""
AskMovies Poster Bot
====================
Monitors log channel → groups files by movie title → fetches poster from TMDB
→ posts formatted caption with lcubots/filestore links to public channel.

Environment Variables (set in Koyeb):
  BOT_TOKEN           = Telegram bot token from @BotFather
  TMDB_API_KEY        = TMDB API key from themoviedb.org
  LOG_CHANNEL_ID      = Channel ID where leech bot posts logs (e.g. -1001234567890)
  PUBLIC_CHANNEL_ID   = Destination public channel ID
  FILESTORE_BOT       = File store bot username (e.g. AskMoviesBot)
  WEBHOOK_URL         = Your Koyeb app URL (e.g. https://your-app.koyeb.app)
  GROUP_WAIT_MINUTES  = Minutes to wait before posting (default: 30)
"""

import os, re, base64, logging, asyncio, json
from datetime import datetime, timezone, timedelta
from collections import defaultdict
import requests
from telegram import Bot, InputMediaPhoto
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
from telegram import Update

# ── Config ────────────────────────────────────────────────────
BOT_TOKEN          = os.environ["BOT_TOKEN"]
TMDB_API_KEY       = os.environ["TMDB_API_KEY"]
LOG_CHANNEL_ID     = os.environ["LOG_CHANNEL_ID"]
PUBLIC_CHANNEL_ID  = os.environ["PUBLIC_CHANNEL_ID"]
FILESTORE_BOT      = os.environ["FILESTORE_BOT"].lstrip("@")
WEBHOOK_URL        = os.environ["WEBHOOK_URL"]
GROUP_WAIT_MINUTES = int(os.environ.get("GROUP_WAIT_MINUTES", "30"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── In-memory store for pending movie groups ──────────────────
# Key: normalized movie title
# Value: {title, year, quality_set, languages, files: [{quality, file_id, link}], timer_task, poster_url}
pending = {}
pending_lock = asyncio.Lock()

# ── TMDB Poster Fetch ─────────────────────────────────────────
def fetch_tmdb_poster(title: str, year: int = None) -> str:
    """Search TMDB for movie/series poster. Returns image URL or None."""
    try:
        params = {"api_key": TMDB_API_KEY, "query": title, "language": "en-US"}
        if year:
            params["year"] = year

        # Try movie first
        r = requests.get("https://api.themoviedb.org/3/search/movie", params=params, timeout=10)
        data = r.json()
        results = data.get("results", [])

        # Try TV series if no movie found
        if not results:
            r = requests.get("https://api.themoviedb.org/3/search/tv", params=params, timeout=10)
            data = r.json()
            results = data.get("results", [])

        if results:
            poster_path = results[0].get("poster_path")
            if poster_path:
                return f"https://image.tmdb.org/t/p/w500{poster_path}"
    except Exception as e:
        log.error(f"TMDB fetch failed: {e}")
    return None

# ── Build lcubots/filestore link from file ID ─────────────────
def make_filestore_link(file_id: str) -> str:
    """Convert numeric file ID to filestore bot link."""
    try:
        encoded = base64.b64encode(file_id.encode()).decode()
        return f"https://t.me/{FILESTORE_BOT}?start=fs_{encoded}"
    except:
        return f"https://t.me/{FILESTORE_BOT}"

# ── Parse log channel message ─────────────────────────────────
def parse_log_message(text: str) -> dict | None:
    """
    Parse log messages like:
      [ASK] Aranmanai 3 (2021) Tamil WEB-DL 1080p.mkv
      Language: Tamil, Telugu
      Quality: #480p
      (file ID on separate message: 22618)

    Also handles just a plain file ID number.
    """
    if not text:
        return None

    result = {}

    # Check if message is JUST a file ID (number only)
    if re.match(r'^\d+$', text.strip()):
        result["file_id"] = text.strip()
        return result

    lines = text.strip().splitlines()

    # Extract filename from first line
    filename = ""
    for line in lines:
        # Remove [ASK] prefix
        clean = re.sub(r'\[.*?\]', '', line).strip()
        if clean and ('.mkv' in clean.lower() or '.mp4' in clean.lower() or '.avi' in clean.lower()):
            filename = clean
            break
        elif clean and not any(k in clean.lower() for k in ['language', 'quality', 'fast', 'join', 'search', 'http']):
            if len(clean) > 10:
                filename = clean

    if not filename and lines:
        filename = re.sub(r'\[.*?\]', '', lines[0]).strip()

    # Extract title and year from filename
    # Pattern: Title (Year) or Title Year
    year_match = re.search(r'\((\d{4})\)', filename)
    if not year_match:
        year_match = re.search(r'\b(20\d{2})\b', filename)

    year = int(year_match.group(1)) if year_match else None

    # Extract title (everything before year)
    if year_match:
        title_raw = filename[:year_match.start()].strip()
    else:
        # Try to extract before quality markers
        title_raw = re.split(r'\b(WEB-DL|HDRip|BluRay|WEBRip|HDCAM|480p|720p|1080p|4K)\b', filename, flags=re.IGNORECASE)[0]

    # Clean title - remove emoji blocks, [ASK] style prefixes, underscores
    title = re.sub(r'[_\-]+', ' ', title_raw).strip()
    title = re.sub(r'[\U0001F300-\U0001FFFF\U00002700-\U000027BF\u2400-\u2BEF]+', '', title).strip()
    title = re.sub(r'[🄰-🅿]+', '', title).strip()  # enclosed letters
    title = re.sub(r'\[.*?\]', '', title).strip()   # [ASK] etc
    title = re.sub(r'\s+', ' ', title).strip()

    # Remove season/episode info from title for grouping
    title_clean = re.sub(r'\s*S\d{1,2}\s*(EP?\d+(-\d+)?)?\s*', ' ', title, flags=re.IGNORECASE).strip()
    title_clean = re.sub(r'\s+', ' ', title_clean).strip()

    if not title_clean:
        return None

    result["title"] = title_clean
    result["year"] = year
    result["filename"] = filename

    # Extract quality from filename or Quality: line
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

    # Extract languages from Language: line
    languages = []
    for line in lines:
        if re.search(r'lang', line, re.IGNORECASE):
            lang_part = re.sub(r'[Ll]ang[a-z]*\s*:', '', line).strip()
            languages = [l.strip() for l in re.split(r'[,+]', lang_part) if l.strip()]
            break

    if not languages:
        # Try to detect from filename
        lang_map = {
            'tam': 'Tamil', 'tel': 'Telugu', 'hin': 'Hindi',
            'eng': 'English', 'mal': 'Malayalam', 'kan': 'Kannada'
        }
        for abbr, lang in lang_map.items():
            if abbr in filename.lower():
                languages.append(lang)

    result["languages"] = languages

    # Detect if series
    is_series = bool(re.search(r'S\d{1,2}\s*EP?\d+', filename, re.IGNORECASE))
    result["is_series"] = is_series

    # Extract quality override (WEB-DL, HDRip, etc.)
    quality_label_match = re.search(r'\b(WEB-DL|HDRip|BluRay|WEBRip|HDCAM|HQ HDRip|TRUE WEB-DL)\b', filename, re.IGNORECASE)
    if quality_label_match:
        result["quality_label"] = quality_label_match.group(1).upper()
    else:
        result["quality_label"] = "WEB-DL"

    return result

# ── Format and send public post ───────────────────────────────
async def post_to_public_channel(bot: Bot, movie_key: str):
    """Format and send the grouped movie post to public channel."""
    async with pending_lock:
        if movie_key not in pending:
            return
        data = pending.pop(movie_key)

    title     = data["title"]
    year      = data.get("year", "")
    languages = data.get("languages", [])
    files     = data.get("files", [])
    is_series = data.get("is_series", False)
    quality_label = data.get("quality_label", "WEB-DL")
    poster_url = data.get("poster_url")

    if not files:
        return

    # Build audio string
    audio_str = " + ".join(languages) if languages else "Tamil"

    # Sort files by quality
    quality_order = {"240p": 1, "360p": 2, "480p": 3, "720p": 4, "1080p": 5, "4K": 6, "2160p": 7}
    files_sorted = sorted(files, key=lambda x: quality_order.get(x.get("quality", ""), 99))

    # Build file lines with hyperlinks
    file_lines = ""
    for f in files_sorted:
        q = f.get("quality", "HD")
        link = f.get("link", "")
        file_lines += f'\n♨️ <a href="{link}">{title} ({year}) - {q}</a>'

    # Build batch link line (use highest quality or last file)
    batch_link = files_sorted[-1].get("link", "") if files_sorted else ""

    # Build caption
    season_line = ""
    if is_series:
        season_match = re.search(r'(S\d{1,2})', data.get("filename", ""), re.IGNORECASE)
        if season_match:
            season_line = f"\n💫 Season: {int(season_match.group(1)[1:])}"

    caption = (
        f"<b>AskMovies</b>\n"
        f"🎬 <b>Title:</b> {title}\n"
        f"📅 <b>Year :</b> {year}"
        f"{season_line}\n"
        f"🎞 <b>Quality:</b> {quality_label}\n"
        f"🎧 <b>Audio:</b> {audio_str}\n\n"
        f"🔺<b>Telegram File</b>🔻"
        f"{file_lines}\n\n"
        f'📦<b>Get all files in one link:</b> <a href="{batch_link}">Click Here</a>\n\n'
        f"Note 💢: If the link is not working, copy it and paste it into your browser.\n\n"
        f"❤️Join » @{FILESTORE_BOT}\n"
    )

    try:
        if poster_url:
            await bot.send_photo(
                chat_id=PUBLIC_CHANNEL_ID,
                photo=poster_url,
                caption=caption,
                parse_mode="HTML"
            )
        else:
            await bot.send_message(
                chat_id=PUBLIC_CHANNEL_ID,
                text=caption,
                parse_mode="HTML",
                disable_web_page_preview=True
            )
        log.info(f"✅ Posted: {title} ({year}) with {len(files)} quality(ies)")
    except Exception as e:
        log.error(f"❌ Failed to post {title}: {e}")

# ── Schedule post after wait time ─────────────────────────────
async def schedule_post(bot: Bot, movie_key: str):
    """Wait GROUP_WAIT_MINUTES then post."""
    await asyncio.sleep(GROUP_WAIT_MINUTES * 60)
    log.info(f"⏰ Timer expired for: {movie_key} — posting now")
    await post_to_public_channel(bot, movie_key)

# ── Handle log channel messages ───────────────────────────────
# Store last parsed movie for file ID association
last_parsed = {}

async def handle_log_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.channel_post or update.message
    if not msg:
        return

    if str(msg.chat.id) != str(LOG_CHANNEL_ID):
        return

    text = msg.text or msg.caption or ""
    if not text.strip():
        return

    log.info(f"📥 Log message: {text[:200]}")

    parsed = parse_log_message(text)
    if not parsed:
        return

    # Case 1: Message is just a file ID number
    if "file_id" in parsed and len(parsed) == 1:
        file_id = parsed["file_id"]
        # Associate with last pending movie for this channel
        if last_parsed.get("chat_id") == str(msg.chat.id):
            last_data = last_parsed.get("data", {})
            title = last_data.get("title")
            if title:
                movie_key = f"{title}_{last_data.get('year', '')}".lower().replace(" ", "_")
                link = make_filestore_link(file_id)
                quality = last_data.get("quality", "HD")

                async with pending_lock:
                    if movie_key in pending:
                        # Add file to existing group
                        pending[movie_key]["files"].append({
                            "quality": quality,
                            "file_id": file_id,
                            "link": link
                        })
                        # Reset timer
                        if pending[movie_key].get("timer_task"):
                            pending[movie_key]["timer_task"].cancel()
                        task = asyncio.create_task(schedule_post(context.bot, movie_key))
                        pending[movie_key]["timer_task"] = task
                        log.info(f"📎 Added file ID {file_id} to {movie_key}")
                    else:
                        # Create new group
                        poster_url = fetch_tmdb_poster(title, last_data.get("year"))
                        task = asyncio.create_task(schedule_post(context.bot, movie_key))
                        pending[movie_key] = {
                            "title": title,
                            "year": last_data.get("year"),
                            "languages": last_data.get("languages", []),
                            "quality_label": last_data.get("quality_label", "WEB-DL"),
                            "is_series": last_data.get("is_series", False),
                            "filename": last_data.get("filename", ""),
                            "files": [{"quality": quality, "file_id": file_id, "link": link}],
                            "poster_url": poster_url,
                            "timer_task": task
                        }
                        log.info(f"🆕 New group: {movie_key} | Poster: {'✅' if poster_url else '❌'}")
        return

    # Case 2: Full movie info message
    title = parsed.get("title")
    if not title:
        return

    # Store for file ID association
    last_parsed["chat_id"] = str(msg.chat.id)
    last_parsed["data"] = parsed

    log.info(f"🎬 Parsed: {title} ({parsed.get('year')}) | {parsed.get('quality')} | {parsed.get('languages')}")

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
  
