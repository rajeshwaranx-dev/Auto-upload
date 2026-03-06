"""
AskMovies Poster Bot
====================
Monitors log channel → posts immediately to public channel.
Each log message = ONE file at ONE quality with ONE inline button.
Multiple messages for the same movie accumulate into one edited post.

Environment Variables:
  BOT_TOKEN          = Telegram bot token
  TMDB_API_KEY       = TMDB API key
  LOG_CHANNEL_ID     = Log channel ID (the private log channel)
  PUBLIC_CHANNEL_ID  = Public destination channel ID
  FILESTORE_BOT      = Filestore bot username (without @)
  WEBHOOK_URL        = Koyeb app URL

Log message format:
  <filename line>         e.g.  Vladimir (2026) S01 720p TRUE WEB-DL.mkv
  Quality : 720p
  Lang : Tamil + Telugu
  [Inline button]  label="Vladimir (2026) S01 720p.mkv"
                   url="https://t.me/YourFilestoreBot?start=FILE_ID"
"""

import os
import re
import logging
import asyncio

import requests
from telegram import Update, Message
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
from telegram.constants import ParseMode

# ── Config ────────────────────────────────────────────────────
BOT_TOKEN         = os.environ["BOT_TOKEN"]
TMDB_API_KEY      = os.environ["TMDB_API_KEY"]
LOG_CHANNEL_ID    = str(os.environ["LOG_CHANNEL_ID"])
PUBLIC_CHANNEL_ID = str(os.environ["PUBLIC_CHANNEL_ID"])
FILESTORE_BOT     = os.environ["FILESTORE_BOT"].lstrip("@")
WEBHOOK_URL       = os.environ["WEBHOOK_URL"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── In-memory state ───────────────────────────────────────────
# posted[movie_key] = {
#   title, year, languages, quality_label, is_series, filename,
#   files: [{display_name, quality, link, file_id}],
#   batch_link, poster_url, message_id
# }
posted: dict = {}
posted_lock  = asyncio.Lock()

# ── Constants ─────────────────────────────────────────────────
QUALITY_ORDER = {
    "240p": 1, "360p": 2, "480p": 3,
    "720p": 4, "1080p": 5, "4K": 6, "2160p": 7,
}

LANG_MAP = {
    "tam": "Tamil",    "tel": "Telugu",    "hin": "Hindi",
    "eng": "English",  "mal": "Malayalam", "kan": "Kannada",
    "mar": "Marathi",  "ben": "Bengali",
}

QUALITY_RE = re.compile(r"\b(240p|360p|480p|720p|1080p|2160p|4K)\b", re.IGNORECASE)

SOURCE_RE = re.compile(
    r"\b(TRUE WEB-DL|WEB-DL|HQ HDRip|HDRip|BluRay|WEBRip|HDCAM|HQ|CAMRip)\b",
    re.IGNORECASE,
)


# ── TMDB ──────────────────────────────────────────────────────
def fetch_tmdb_poster(title: str, year: int | None = None) -> str | None:
    try:
        params = {"api_key": TMDB_API_KEY, "query": title, "language": "en-US"}
        if year:
            params["year"] = year
        for endpoint in ("movie", "tv"):
            r = requests.get(
                f"https://api.themoviedb.org/3/search/{endpoint}",
                params=params,
                timeout=10,
            )
            r.raise_for_status()
            results = r.json().get("results", [])
            if results and results[0].get("poster_path"):
                return f"https://image.tmdb.org/t/p/w500{results[0]['poster_path']}"
    except Exception as exc:
        log.warning("TMDB lookup failed for %r: %s", title, exc)
    return None


# ── Helpers ───────────────────────────────────────────────────
def clean_first_line(raw: str) -> str:
    """Strip [TAG] blocks and stray non-Latin characters."""
    raw = re.sub(r"\[.*?\]", "", raw)
    raw = re.sub(r"[^\x00-\u024F\s()\[\]\-_.]+", "", raw)
    return raw.strip()


def extract_title_year(filename: str) -> tuple[str, int | None]:
    YEAR_RE   = re.compile(r"\((\d{4})\)|\b(20\d{2})\b")
    SPLIT_PAT = re.compile(
        r"\b(WEB-DL|HDRip|BluRay|WEBRip|HDCAM|480p|720p|1080p|4K|HQ|CAMRip)\b",
        re.IGNORECASE,
    )
    year: int | None = None
    m = YEAR_RE.search(filename)
    if m:
        year      = int(m.group(1) or m.group(2))
        title_raw = filename[: m.start()]
    else:
        title_raw = SPLIT_PAT.split(filename)[0]

    title = re.sub(r"[_\-]+", " ", title_raw)
    title = re.sub(r"\.(mkv|mp4|avi|mov)$", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\s*S\d{1,2}E?\d*\s*", " ", title, flags=re.IGNORECASE)
    title = re.sub(r"\s+", " ", title).strip()
    return title, year


def quality_from_text(text: str) -> str:
    m = QUALITY_RE.search(text)
    return m.group(1) if m else "HD"


def file_id_from_url(url: str) -> str:
    """Extract the start= parameter value as a stable dedup key."""
    m = re.search(r"[?&]start=([^&\s]+)", url)
    return m.group(1) if m else url


# ── Single-button extractor ───────────────────────────────────
def extract_file_entry(text: str, reply_markup) -> dict | None:
    """
    Each log message has ONE inline button:
      label = filename  (e.g. "Vladimir (2026) S01 720p WEB-DL.mkv")
      url   = https://t.me/FilestoreBot?start=FILE_ID

    Returns {display_name, quality, link, file_id} or None.
    """
    # ── Primary: inline keyboard button ───────────────────────
    if reply_markup and hasattr(reply_markup, "inline_keyboard"):
        for row in reply_markup.inline_keyboard:
            for btn in row:
                url   = getattr(btn, "url", None)
                label = (btn.text or "").strip()
                if url and "t.me/" in url and label:
                    entry = {
                        "display_name": label,
                        "quality":      quality_from_text(label),
                        "link":         url,
                        "file_id":      file_id_from_url(url),
                    }
                    log.info("Button → %s | %s", label, url)
                    return entry   # one button per message

    # ── Fallback: bare t.me link in text ──────────────────────
    m = re.search(r"(https?://t\.me/\S+)", text)
    if m:
        url   = m.group(1)
        first = text.splitlines()[0].strip()
        label = first if re.search(r"\.(mkv|mp4|avi|mov)\b", first, re.IGNORECASE) else url
        log.info("Text fallback → %s | %s", label, url)
        return {
            "display_name": label,
            "quality":      quality_from_text(label),
            "link":         url,
            "file_id":      file_id_from_url(url),
        }

    log.warning("No button and no t.me link found — message skipped")
    return None


# ── Parser ────────────────────────────────────────────────────
def parse_log_message(text: str, reply_markup=None) -> dict | None:
    if not text or not text.strip():
        return None

    lines      = text.strip().splitlines()
    first_line = clean_first_line(lines[0])
    title, year = extract_title_year(first_line)

    if not title or len(title) < 2:
        log.debug("Skipping — title too short from %r", lines[0])
        return None

    # Quality label (source type)
    m = SOURCE_RE.search(first_line)
    quality_label = m.group(1).upper() if m else "WEB-DL"

    # Languages
    languages: list[str] = []
    for line in lines:
        if re.search(r"lang", line, re.IGNORECASE):
            lang_part = re.sub(r"[Ll]ang[a-z]*\s*:\s*", "", line).strip()
            languages = [lx.strip() for lx in re.split(r"[,+&/]", lang_part) if lx.strip()]
            break
    if not languages:
        fn_lower = first_line.lower()
        languages = [name for abbr, name in LANG_MAP.items() if abbr in fn_lower]

    # Series detection
    is_series = bool(re.search(r"\bS\d{1,2}\s*E?P?\d+\b", first_line, re.IGNORECASE))

    # Single file entry from the one button
    file_entry = extract_file_entry(text, reply_markup)

    return {
        "title":         title,
        "year":          year,
        "filename":      first_line,
        "quality_label": quality_label,
        "languages":     languages,
        "is_series":     is_series,
        "file_entry":    file_entry,   # may be None if no button found
    }


# ── Caption builder ───────────────────────────────────────────
def build_caption(data: dict) -> str:
    title         = data["title"]
    year          = data.get("year", "")
    languages     = data.get("languages") or []
    files         = data.get("files") or []
    is_series     = data.get("is_series", False)
    quality_label = data.get("quality_label", "WEB-DL")
    filename      = data.get("filename", "")

    audio_str = " + ".join(languages) if languages else "Tamil"

    files_sorted = sorted(
        files,
        key=lambda f: QUALITY_ORDER.get(f.get("quality", ""), 99),
    )

    # One clickable line per quality — button URL goes directly to filestore bot
    file_lines = ""
    for f in files_sorted:
        label = f.get("display_name") or f.get("quality", "HD")
        link  = f["link"]
        file_lines += f'\n🔥 <a href="{link}">{label}</a>'

    # Batch link = highest quality file's link
    batch_link = files_sorted[-1]["link"] if files_sorted else f"https://t.me/{FILESTORE_BOT}"

    season_line = ""
    if is_series:
        sm = re.search(r"S(\d{1,2})", filename, re.IGNORECASE)
        if sm:
            season_line = f"\n💫 <b>Season:</b> {int(sm.group(1))}"

    return (
        "<b>AskMovies</b>\n"
        f"🎬 <b>Title:</b> {title}\n"
        f"📅 <b>Year :</b> {year}"
        f"{season_line}\n"
        f"🎞 <b>Quality:</b> {quality_label}\n"
        f"🎧 <b>Audio:</b> {audio_str}\n\n"
        "🔺<b>Telegram File</b>🔻"
        f"{file_lines}\n\n"
        f'📦 <b>Get all files in one link:</b> <a href="{batch_link}">Click Here</a>\n\n'
        "Note 💢: If the link is not working, copy it and paste it into your browser.\n\n"
        f"❤️Join » @{FILESTORE_BOT}"
    )


# ── Deduplication ─────────────────────────────────────────────
def already_stored(files: list[dict], file_id: str) -> bool:
    """Deduplicate by file_id (the start= parameter), not full URL."""
    return any(f.get("file_id") == file_id for f in files)


# ── Handler ───────────────────────────────────────────────────
async def handle_log_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg: Message | None = update.channel_post or update.message
    if not msg:
        return
    if str(msg.chat.id) != LOG_CHANNEL_ID:
        return

    text         = (msg.text or msg.caption or "").strip()
    reply_markup = msg.reply_markup  # InlineKeyboardMarkup or None

    if not text:
        return

    log.info("📥 (%d chars): %s", len(text), text[:160])

    parsed = parse_log_message(text, reply_markup)
    if not parsed:
        return

    title      = parsed["title"]
    year       = parsed.get("year")
    movie_key  = re.sub(r"\s+", "_", f"{title}_{year or ''}".lower())
    file_entry = parsed.get("file_entry")   # single {display_name, quality, link, file_id}

    async with posted_lock:

        if movie_key in posted:
            # ── Movie already posted → add new quality if not duplicate ──
            data = posted[movie_key]

            if file_entry:
                if already_stored(data["files"], file_entry["file_id"]):
                    log.info("⏭ Duplicate file_id for %r — skipping", title)
                    return
                data["files"].append(file_entry)
            else:
                log.info("⏭ No file entry in message for %r — skipping edit", title)
                return

            caption = build_caption(data)
            try:
                await context.bot.edit_message_caption(
                    chat_id=PUBLIC_CHANNEL_ID,
                    message_id=data["message_id"],
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                )
                log.info("✏️  Edited %r — added %s", title, file_entry["quality"])
            except Exception as exc:
                log.error("Edit failed for %r (msg_id=%s): %s", title, data["message_id"], exc)

        else:
            # ── New movie → fetch poster and post ─────────────────────
            poster_url = fetch_tmdb_poster(title, year)

            data = {
                "title":         title,
                "year":          year,
                "languages":     parsed.get("languages", []),
                "quality_label": parsed.get("quality_label", "WEB-DL"),
                "is_series":     parsed.get("is_series", False),
                "filename":      parsed.get("filename", ""),
                "files":         [file_entry] if file_entry else [],
                "poster_url":    poster_url,
                "message_id":    None,
            }

            caption = build_caption(data)
            try:
                if poster_url:
                    sent = await context.bot.send_photo(
                        chat_id=PUBLIC_CHANNEL_ID,
                        photo=poster_url,
                        caption=caption,
                        parse_mode=ParseMode.HTML,
                    )
                else:
                    sent = await context.bot.send_message(
                        chat_id=PUBLIC_CHANNEL_ID,
                        text=caption,
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True,
                    )

                data["message_id"] = sent.message_id
                posted[movie_key]  = data
                log.info(
                    "✅ Posted %r (%s) | quality=%s | msg_id=%s",
                    title, year,
                    file_entry["quality"] if file_entry else "—",
                    sent.message_id,
                )
            except Exception as exc:
                log.error("Post failed for %r: %s", title, exc)


# ── Entry point ───────────────────────────────────────────────
if __name__ == "__main__":
    port         = int(os.environ.get("PORT", 8000))
    webhook_path = f"/webhook/{BOT_TOKEN}"
    full_webhook = f"{WEBHOOK_URL.rstrip('/')}{webhook_path}"

    log.info("🤖 AskMovies Poster Bot starting (webhook) on port %d", port)

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.ALL, handle_log_message))
    app.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=webhook_path,
        webhook_url=full_webhook,
              )
  
