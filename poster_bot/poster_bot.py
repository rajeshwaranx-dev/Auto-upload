"""
AskMovies Poster Bot
====================
Monitors log channel → posts to public channel once the button arrives.

How Telegram delivers log messages:
  1. channel_post arrives  → text only, NO button yet
  2. edited_channel_post   → same message, NOW has the inline button with the URL

Strategy:
  - On channel_post:       parse title/year/quality, store as "pending"
  - On edited_channel_post: read the button URL, then post (or edit) the public channel

Environment Variables:
  BOT_TOKEN          = Telegram bot token
  TMDB_API_KEY       = TMDB API key
  LOG_CHANNEL_ID     = Log channel ID (private log channel)
  PUBLIC_CHANNEL_ID  = Public destination channel ID
  FILESTORE_BOT      = Filestore bot username (without @)
  WEBHOOK_URL        = Koyeb app URL

Log message format (one message per quality):
  [ASK] Granny (2026) Tamil TRUE WEB-DL 1080p AVC.mkv
  Langauge : Tamil
  Quality : 1080p
  [Button added via edit]  label="Granny (2026) Tamil 1080p.mkv"
                           url="https://Askmovies.lcubots.news/?start=fs_MjI2NzA="
"""

import os
import re
import logging
import asyncio

import requests
from telegram import Update, Message
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    filters,
    ContextTypes,
)
from telegram.constants import ParseMode

# ── Config ────────────────────────────────────────────────────
BOT_TOKEN         = os.environ["BOT_TOKEN"]
TMDB_API_KEY      = os.environ["TMDB_API_KEY"]
LOG_CHANNEL_ID    = str(os.environ["LOG_CHANNEL_ID"])
PUBLIC_CHANNEL_ID = str(os.environ["PUBLIC_CHANNEL_ID"])
FILESTORE_BOT     = os.environ["FILESTORE_BOT"].lstrip("@")
WEBHOOK_URL       = os.environ.get("WEBHOOK_URL", "")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── In-memory state ───────────────────────────────────────────
# pending[log_msg_id] = {title, year, quality_label, languages, is_series, filename}
#   Stored on channel_post, consumed on edited_channel_post.
pending: dict = {}

# posted[movie_key] = {title, year, ..., files:[...], message_id, poster_url}
#   One entry per unique movie — accumulates all quality variants.
posted: dict = {}

state_lock = asyncio.Lock()

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
SOURCE_RE  = re.compile(
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
                params=params, timeout=10,
            )
            r.raise_for_status()
            results = r.json().get("results", [])
            if results and results[0].get("poster_path"):
                return f"https://image.tmdb.org/t/p/w500{results[0]['poster_path']}"
    except Exception as exc:
        log.warning("TMDB error for %r: %s", title, exc)
    return None


# ── Text helpers ──────────────────────────────────────────────
# Matches [ASK], [A|S|K], boxed-letter tags at the START of the line only
ASK_TAG_RE = re.compile(r"^(\s*\[[A-Z|\s]{1,10}\]\s*)+", re.IGNORECASE)

def clean_first_line(raw: str) -> str:
    # Only strip leading [ASK]-style tags, NOT language tags like [Tamil + Malayalam]
    raw = ASK_TAG_RE.sub("", raw)
    # Strip emoji and non-Latin characters but keep brackets, so [Tamil + Malayalam] survives
    raw = re.sub(r"[^\x00-\u024F\s()\[\]\-_+.]+", "", raw)
    return raw.strip()


def extract_title_year(filename: str) -> tuple[str, int | None]:
    YEAR_RE   = re.compile(r"\((\d{4})\)|\b(20\d{2})\b")
    SPLIT_PAT = re.compile(
        r"\b(WEB-DL|HDRip|BluRay|WEBRip|HDCAM|480p|720p|1080p|4K|HQ|CAMRip|TRUE)\b",
        re.IGNORECASE,
    )
    # Episode pattern — split title here so episode titles don't bleed in
    EP_SPLIT  = re.compile(r"\bS\d{1,2}\s*E(?:P)?\d+\b|\bEP?\s*\d{1,3}\b", re.IGNORECASE)

    year: int | None = None
    m = YEAR_RE.search(filename)
    if m:
        year      = int(m.group(1) or m.group(2))
        title_raw = filename[: m.start()]
    else:
        # Try splitting on episode pattern first, then quality keywords
        ep_m = EP_SPLIT.search(filename)
        if ep_m:
            title_raw = filename[: ep_m.start()]
        else:
            title_raw = SPLIT_PAT.split(filename)[0]

    title = re.sub(r"[_\-]+", " ", title_raw)
    title = re.sub(r"\.(mkv|mp4|avi|mov)$", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\s*S\d{1,2}E?\d*\s*", " ", title, flags=re.IGNORECASE)
    title = re.sub(r"\s+", " ", title).strip()
    return title, year


def quality_from_text(text: str) -> str:
    clean = text.replace("\xa0", " ")
    m = QUALITY_RE.search(clean)
    return m.group(1) if m else "HD"

def file_id_from_url(url: str) -> str:
    """Stable dedup key — the start= value, or the full URL if absent."""
    m = re.search(r"[?&]start=([^&\s]+)", url)
    return m.group(1) if m else url


# ── Parse the initial (text-only) channel_post ───────────────
def parse_initial_message(text: str) -> dict | None:
    """
    Called on channel_post (no button yet).
    Extracts title, year, quality_label, languages, is_series, filename.
    """
    if not text or not text.strip():
        return None

    lines      = text.strip().splitlines()
    first_line = clean_first_line(lines[0])
    title, year = extract_title_year(first_line)

    if not title or len(title) < 2:
        log.debug("Skipping — bad title from %r", lines[0])
        return None

    m = SOURCE_RE.search(first_line)
    quality_label = m.group(1).upper() if m else "WEB-DL"

    # Resolution from explicit "Quality :" line, else filename
    quality = ""
    for line in lines:
        qm = re.search(r"Quality\s*:\s*#?(\S+)", line, re.IGNORECASE)
        if qm:
            quality = qm.group(1).lstrip("#")
            break
    if not quality:
        quality = quality_from_text(first_line)

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

    is_series = bool(
        re.search(r"\bS\d{1,2}\s*E?P?\d+\b", first_line, re.IGNORECASE) or
        re.search(r"\bEP?\s*\(?\d", first_line, re.IGNORECASE)   # EP01 or EP (01-08)
    )

    return {
        "title":         title,
        "year":          year,
        "filename":      first_line,
        "quality":       quality,
        "quality_label": quality_label,
        "languages":     languages,
        "is_series":     is_series,
    }


# Generic button labels that carry no useful filename info
GENERIC_LABELS = re.compile(
    r"^(get\s+shar(e?able|ing)|download|click\s+here|open|get\s+file|watch|stream)\b",
    re.IGNORECASE,
)


def resolve_display_name(btn_label: str, meta: dict) -> str:
    """
    Use the button label as display name ONLY if it looks like a real filename.
    Otherwise fall back to the filename stored in meta (from the message text).
    """
    if not GENERIC_LABELS.match(btn_label):
        return btn_label          # label is a proper filename — use it
    # Generic label — build display name from meta filename
    return meta.get("filename") or btn_label


# ── Read button URL from edited_channel_post ─────────────────
def extract_button_entry(text: str, reply_markup, meta: dict) -> dict | None:
    """
    Called on edited_channel_post (button now present).
    Returns {display_name, quality, link, file_id}.
    """
    # Primary: inline keyboard button — accept any https URL
    if reply_markup and hasattr(reply_markup, "inline_keyboard"):
        for row in reply_markup.inline_keyboard:
            for btn in row:
                url      = getattr(btn, "url", None)
                btn_text = (btn.text or "").strip()
                if url and url.startswith("http") and btn_text:
                    display = resolve_display_name(btn_text, meta)
                    # Quality: try filename first, then fall back to the
                    # explicitly parsed "Quality : 1080p" line from meta
                    quality = quality_from_text(display)
                    if not quality or quality == "HD":
                        quality = meta.get("quality") or "HD"
                    log.info("Button → display=%r quality=%s url=%s", display, quality, url)
                    entry = {
                        "display_name": display,
                        "quality":      quality,
                        "link":         url,
                        "file_id":      file_id_from_url(url),
                    }
                    # Store ep number for dedup
                    ep_m = EP_RE.search(display)
                    entry["ep"] = int(ep_m.group(1) or ep_m.group(2)) if ep_m else None
                    return entry

    log.warning("No button URL on edited message — cannot get file link")
    return None


# ── Episode extractor ────────────────────────────────────────
EP_RE = re.compile(r"\bS\d{1,2}E(\d{1,3})\b|\bEP?\s*(\d{1,3})\b", re.IGNORECASE)

def ep_num(f: dict) -> int | None:
    """Return episode number from display_name, or None.
    Handles both EP03 and S01E03 formats.
    """
    m = EP_RE.search(f.get("display_name") or "")
    if not m:
        return None
    # group(1) = SxxExx capture, group(2) = EP/E capture
    return int(m.group(1) or m.group(2))


def build_series_file_lines(files: list[dict]) -> tuple[str, str]:
    """
    Group files by episode, show qualities as pipe-separated links per episode.
    Returns (file_lines_html, batch_qualities_html).

    Example line:  🌊 EP01 : <a href="...">480P</a> | <a href="...">720P</a> | <a href="...">1080P</a>
    """
    # Group: ep_num → list of (quality, link)
    from collections import defaultdict
    episodes: dict = defaultdict(list)
    no_ep = []

    for f in files:
        eq = ep_num(f)
        quality = f.get("quality", "HD")
        link    = f["link"]
        if eq is not None:
            episodes[eq].append((quality, link))
        else:
            no_ep.append(f)

    parts = []

    # Episode grouped lines
    for ep in sorted(episodes.keys()):
        quals = sorted(
            episodes[ep],
            key=lambda x: QUALITY_ORDER.get(x[0], 99),
        )
        qual_links = " | ".join(f'<a href="{lnk}">{q}</a>' for q, lnk in quals)
        parts.append(f"<b>🌊 EP{ep:02d} : {qual_links}</b>")

    # Files without episode number (movies mixed in)
    for f in no_ep:
        label = f.get("display_name") or f.get("quality", "HD")
        lnk = f['link']
        parts.append(f'<b>🔥 <a href="{lnk}">{label}</a></b>')

    # Episodes: single newline (no blank line between them)
    # no_ep files (batch packs): double newline
    ep_parts  = [p for p in parts if p.startswith("🌊")]
    nep_parts = [p for p in parts if not p.startswith("🌊")]
    combined  = []
    if ep_parts:
        combined.append("\n".join(ep_parts))
    combined.extend(nep_parts)
    file_lines = "\n\n".join(combined)
    if file_lines:
        file_lines = "\n" + file_lines

    # Batch line: all unique qualities as links to highest-quality file each
    best: dict = {}
    for f in files:
        q = f.get("quality", "HD")
        if q not in best or QUALITY_ORDER.get(q, 99) > QUALITY_ORDER.get(best[q].get("quality",""), 99):
            best[q] = f
    batch_parts = sorted(best.values(), key=lambda f: QUALITY_ORDER.get(f.get("quality",""), 99))
    batch_str = " | ".join('<a href="' + f['link'] + '">' + f.get("quality","HD") + '</a>' for f in batch_parts)

    return file_lines, batch_str


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

    batch_link = (
        files_sorted[-1]["link"] if files_sorted
        else f"https://t.me/{FILESTORE_BOT}"
    )

    season_line = ""
    if is_series:
        sm = re.search(r"S(\d{1,2})", filename, re.IGNORECASE)
        if sm:
            season_line = f"\n💫 <b>Season: {int(sm.group(1))}</b>"

    # Also detect series from file display_names (e.g. S01E03 in filename)
    has_ep_files = any(ep_num(f) is not None for f in files)
    if not has_ep_files:
        # Check display_name for SxxExx pattern too
        has_ep_files = any(
            re.search(r"S\d{1,2}E\d{1,3}", f.get("display_name",""), re.IGNORECASE)
            for f in files
        )
    if is_series and has_ep_files:
        # ── Series format: EP01 : 480P | 720P | 1080P ─────────
        file_lines, batch_str = build_series_file_lines(files)
        batch_section = f'📦 Get all files for: {batch_str}'
    else:
        # ── Movie format: one 🔥 line per file ─────────────────
        file_parts = []
        for f in files_sorted:
            label = f.get("display_name") or f.get("quality", "HD")
            lnk2 = f['link']
            file_parts.append(f'<b>🔥 <a href="{lnk2}">{label}</a></b>')
        file_lines = "\n\n".join(file_parts)
        if file_lines:
            file_lines = "\n" + file_lines
        batch_section = f'📦 Get all files in one link: <a href="{batch_link}">Click Here</a>'

    return (
        f'<a href="https://t.me/{FILESTORE_BOT}"><b>AskMovies</b></a>\n'
        f"🎬 <b>Title: {title}</b>\n"
        f"📅 <b>Year : {year or 'N/A'}</b>"
        f"{season_line}\n"
        f"🎞 <b>Quality: {quality_label}</b>\n"
        f"🎧 <b>Audio: {audio_str}</b>\n\n"
        "<b>🔺Telegram File🔻</b>\n"
        f"{file_lines}\n\n"
        f"<b>{batch_section}</b>\n\n"
        "<b>Note 💢: If the link is not working, copy it and paste it into your browser.</b>\n\n"
        f"<b>❤️Join » @{FILESTORE_BOT}</b>"
    )


def already_stored(files: list[dict], file_id: str, ep: int | None, quality: str, display_name: str = "") -> bool:
    """
    Deduplicate criteria:
      1. Exact file_id match    — same file re-uploaded (always checked)
      2. Same ep + quality      — series: skip same episode+quality
      3. Same display_name      — movies only: skip same filename
                                  (skipped for series since same filename = different quality)
    """
    is_series_file = ep is not None
    for f in files:
        if f.get("file_id") == file_id:
            return True
        if is_series_file and f.get("ep") == ep and f.get("quality") == quality:
            return True
        if not is_series_file and display_name and f.get("display_name") == display_name:
            return True
    return False


def movie_key_for(title: str, year, languages: list, is_series: bool = False) -> str:
    # For series: key = title + year only (all langs/qualities go in one post)
    # For movies: key = title + year + primary_lang (separate post per language)
    if is_series:
        return re.sub(r"\s+", "_", f"{title}_{year or ''}".lower())
    lang = languages[0].lower() if languages else "unknown"
    return re.sub(r"\s+", "_", f"{title}_{year or ''}_{lang}".lower())


# ── Handler: initial message (text only, no button) ──────────
async def handle_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg: Message | None = update.channel_post
    if not msg or str(msg.chat.id) != LOG_CHANNEL_ID:
        return

    text = (msg.text or msg.caption or "").strip()
    if not text:
        return

    log.info("📥 new msg_id=%d: %s", msg.message_id, text[:120])

    parsed = parse_initial_message(text)
    if not parsed:
        return

    async with state_lock:
        pending[msg.message_id] = parsed
        log.info("⏳ Pending msg_id=%d → %r (%s)", msg.message_id, parsed["title"], parsed["year"])


# ── Handler: edited message (button now attached) ─────────────
async def handle_edited_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg: Message | None = update.edited_channel_post
    if not msg or str(msg.chat.id) != LOG_CHANNEL_ID:
        return

    text         = (msg.text or msg.caption or "").strip()
    reply_markup = msg.reply_markup

    log.info("✏️ edited msg_id=%d buttons=%s", msg.message_id,
             bool(reply_markup and getattr(reply_markup, "inline_keyboard", None)))

    async with state_lock:
        meta = pending.pop(msg.message_id, None)

    if not meta:
        # Edit arrived without a prior channel_post in memory — parse text now
        log.info("No pending entry for msg_id=%d — parsing text", msg.message_id)
        meta = parse_initial_message(text)
        if not meta:
            return

    log.info("🔍 meta filename=%r", meta.get("filename"))
    file_entry = extract_button_entry(text, reply_markup, meta)
    if not file_entry:
        return
    log.info("🔍 file_entry display_name=%r", file_entry.get("display_name"))

    title     = meta["title"]
    year      = meta.get("year")
    mkey      = movie_key_for(title, year, meta.get("languages", []), meta.get("is_series", False))

    async with state_lock:
        if mkey in posted:
            # ── Add quality to existing public post ───────────
            data = posted[mkey]
            ep_no = ep_num(file_entry)
            if already_stored(data["files"], file_entry["file_id"], ep_no, file_entry["quality"], file_entry.get("display_name", "")):
                log.info("⏭ Duplicate ep=%s quality=%s name=%r for %r — skipping", ep_no, file_entry["quality"], file_entry.get("display_name",""), title)
                return
            data["files"].append(file_entry)

            caption = build_caption(data)
            try:
                if data.get("has_photo"):
                    await context.bot.edit_message_caption(
                        chat_id=PUBLIC_CHANNEL_ID,
                        message_id=data["message_id"],
                        caption=caption,
                        parse_mode=ParseMode.HTML,
                    )
                else:
                    await context.bot.edit_message_text(
                        chat_id=PUBLIC_CHANNEL_ID,
                        message_id=data["message_id"],
                        text=caption,
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True,
                    )
                log.info("✏️  Public post edited for %r — added %s", title, file_entry["quality"])
            except Exception as exc:
                log.error("Edit failed for %r: %s", title, exc)

        else:
            # ── Create new public post ────────────────────────
            poster_url = fetch_tmdb_poster(title, year)

            data = {
                "title":         title,
                "year":          year,
                "languages":     meta.get("languages", []),
                "quality_label": meta.get("quality_label", "WEB-DL"),
                "is_series":     meta.get("is_series", False),
                "filename":      meta.get("filename", ""),
                "files":         [file_entry],
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
                data["has_photo"]  = bool(poster_url)
                posted[mkey]       = data
                log.info("✅ Posted %r (%s) | %s | msg_id=%s",
                         title, year, file_entry["quality"], sent.message_id)
            except Exception as exc:
                log.error("Post failed for %r: %s", title, exc)


# ── Entry point ───────────────────────────────────────────────
if __name__ == "__main__":
    use_webhook = bool(os.environ.get("WEBHOOK_URL", "").strip())

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Two separate handlers — one for new posts, one for edits
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL & ~filters.UpdateType.EDITED, handle_channel_post))
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL & filters.UpdateType.EDITED, handle_edited_post))

    if use_webhook:
        port         = int(os.environ.get("PORT", 8000))
        webhook_path = f"/webhook/{BOT_TOKEN}"
        full_webhook = f"{os.environ['WEBHOOK_URL'].rstrip('/')}{webhook_path}"
        log.info("🤖 AskMovies Poster Bot starting (webhook) on port %d", port)
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=webhook_path,
            webhook_url=full_webhook,
        )
    else:
        log.info("🤖 AskMovies Poster Bot starting (polling)")
        app.run_polling(drop_pending_updates=True)
