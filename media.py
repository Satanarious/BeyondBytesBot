"""Media & book lookups for BBbot.

Commands — for ALL members (group-locked, NOT admin-only):
  /anime /eac        — MyAnimeList (Jikan) with AniList GraphQL fallback
  /books /book       — OpenLibrary
  /comics /comic     — ComicVine (with COMICVINE_API_KEY) → OpenLibrary fallback

/eac searches manga/manhwa/manhua on MAL, falling back to AniList. Results
render as one rich message: cover art inline, title embedded as a link,
rating + counts, short synopsis. Falls back to photo+caption, then plain text.
"""

import html as _html
import logging
import os
import re

import aiohttp
from telegram import LinkPreviewOptions, Update
from telegram.constants import ParseMode
from telegram.ext import CommandHandler, ContextTypes

from guard import group_command
from utils import get_thread_id

log = logging.getLogger("bbbot.media")

_HTTP = aiohttp.ClientTimeout(total=15, connect=7)
_UA = "BBbot/1.0 (Telegram media lookup bot)"
_SYN_MAX = 480

_ANILIST_URL = "https://graphql.anilist.co"
_JIKAN_URL = "https://api.jikan.moe/v4"
_OL_URL = "https://openlibrary.org"
_CV_URL = "https://comicvine.gamespot.com/api"

_ANILIST_QUERY = """
query ($q: String!, $type: MediaType) {
  Page(perPage: 3) {
    media(search: $q, type: $type, sort: SEARCH_MATCH) {
      title { english romaji native }
      averageScore
      episodes
      chapters
      volumes
      format
      seasonYear
      description
      siteUrl
      coverImage { large }
    }
  }
}
"""


def _strip_html(value):
    text = re.sub(r"<[^>]+>", " ", str(value or ""))
    text = _html.unescape(text)
    return " ".join(text.split())


def _synopsis(value, limit=_SYN_MAX):
    text = _strip_html(value)
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "…"
    return text


async def _get_json(session, url, params=None, json_body=None, headers=None):
    """JSON request wrapper; raises on non-200 so callers can classify."""
    async with session.get(
        url, params=params, json=json_body, headers=headers
    ) as resp:
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status} from the lookup source")
        return await resp.json(content_type=None)


# ------------------------------------------------------------ Jikan / MAL ---

async def _jikan_search(session, kind, q):
    """Search MyAnimeList via Jikan. kind: 'anime' or 'manga'."""
    data = await _get_json(
        session, f"{_JIKAN_URL}/{kind}",
        params={"q": q, "limit": 3},
    )
    rows = []
    for item in (data.get("data") or [])[:3]:
        image = ((item.get("images") or {}).get("jpg") or {}).get("large_image_url", "")
        counts = []
        if kind == "anime" and item.get("episodes"):
            counts.append(f"{item['episodes']} episodes")
        if kind == "manga" and item.get("chapters"):
            counts.append(f"{item['chapters']} chapters")
        if kind == "manga" and item.get("volumes"):
            counts.append(f"{item['volumes']} volumes")
        rows.append({
            "title": item.get("title") or "Unknown title",
            "url": item.get("url") or f"https://myanimelist.net/{kind}/",
            "image": image or "",
            "rating": item.get("score"),
            "details": " · ".join(counts),
            "synopsis": _synopsis(item.get("synopsis")),
        })
    return rows


# ------------------------------------------------------------- AniList ------

def _anilist_rows(data, _seen=[]):
    rows = []
    for item in (((data.get("data") or {}).get("Page") or {}).get("media") or [])[:3]:
        title = item.get("title") or {}
        name = (title.get("english") or title.get("romaji")
                or title.get("native") or "Unknown title")
        counts = []
        if item.get("episodes"):
            counts.append(f"{item['episodes']} episodes")
        if item.get("chapters"):
            counts.append(f"{item['chapters']} chapters")
        if item.get("volumes"):
            counts.append(f"{item['volumes']} volumes")
        rows.append({
            "title": name,
            "url": item.get("siteUrl") or "https://anilist.co",
            "image": (item.get("coverImage") or {}).get("large", ""),
            "rating": item.get("averageScore"),
            "details": " · ".join(counts),
            "synopsis": _synopsis(item.get("description")),
        })
    return rows


async def _anilist_search(session, q, media_type):
    data = await _get_json(
        session, _ANILIST_URL,
        json_body={"query": _ANILIST_QUERY, "variables": {"q": q, "type": media_type}},
    )
    return _anilist_rows(data)


# ----------------------------------------------------------- ComicVine ------

async def _comicvine_search(session, q, api_key):
    data = await _get_json(
        session, f"{_CV_URL}/volumes/",
        params={
            "api_key": api_key,
            "format": "json",
            "q": q,
            "limit": 3,
            "field_list": "name,deck,image,count_of_issues,start_year,site_detail_url",
        },
    )
    rows = []
    for item in (data.get("results") or [])[:3]:
        image = (item.get("image") or {}).get("medium_url") or \
                (item.get("image") or {}).get("thumb_url") or ""
        rows.append({
            "title": _strip_html(item.get("name")) or "Unknown series",
            "url": item.get("site_detail_url") or "https://comicvine.gamespot.com",
            "image": image or "",
            "rating": None,          # ComicVine volumes carry no simple rating
            "details": " · ".join(
                part for part in (
                    f"{item['count_of_issues']} issues" if item.get("count_of_issues") else "",
                    f"since {item['start_year']}" if item.get("start_year") else "",
                ) if part
            ),
            "synopsis": _synopsis(item.get("deck"), 300),
        })
    return rows


# ------------------------------------------------------------ OpenLibrary ---

async def _openlibrary_search(session, q, subject=None):
    params = {"q": q, "limit": 3}
    if subject:
        params["subject"] = subject
    data = await _get_json(session, f"{_OL_URL}/search.json", params=params)
    rows = []
    for item in (data.get("docs") or [])[:3]:
        work_key = item.get("key") or ""
        url = f"https://openlibrary.org{work_key}" if work_key else _OL_URL
        authors = ", ".join((item.get("author_name") or [])[:3])
        rows.append({
            "title": item.get("title") or "Unknown title",
            "url": url,
            "image": (
                f"https://covers.openlibrary.org/b/id/{item['cover_i']}-L.jpg"
                if item.get("cover_i") else ""
            ),
            "rating": item.get("ratings_average"),
            "details": " · ".join(
                part for part in (
                    f"by {authors}" if authors else "",
                    f"{item.get('first_publish_year', '')}" if item.get("first_publish_year") else "",
                    f"{item['edition_count']} editions" if item.get("edition_count") else "",
                ) if part
            ),
            "synopsis": "",          # search.json does not carry plot text
        })
    return rows


# ------------------------------------------------------------- lookups ------

async def _lookup_anime(session, q):
    """MAL first, AniList fallback. Returns (rows, source_note)."""
    try:
        return await _jikan_search(session, "anime", q), None
    except Exception as exc:
        log.info("Jikan anime failed for %r (%s); trying AniList", q, exc)
    rows = await _anilist_search(session, q, "ANIME")
    return rows, "via AniList"


async def _lookup_manga(session, q):
    """MAL manga (incl. manhwa/manhua) first, AniList fallback."""
    try:
        return await _jikan_search(session, "manga", q), None
    except Exception as exc:
        log.info("Jikan manga failed for %r (%s); trying AniList", q, exc)
    rows = await _anilist_search(session, q, "MANGA")
    return rows, "via AniList"


async def _lookup_comic(session, q, note=None):
    """ComicVine when configured, OpenLibrary graphic-novels otherwise."""
    api_key = (os.getenv("COMICVINE_API_KEY") or "").strip()
    if api_key:
        try:
            rows = await _comicvine_search(session, q, api_key)
            if rows:
                return rows, None
            log.info("ComicVine: no results for %r; trying OpenLibrary", q)
        except Exception as exc:
            log.info("ComicVine failed for %r (%s); trying OpenLibrary", q, exc)
        try:
            return await _openlibrary_search(session, q, subject="graphic novels"), "via OpenLibrary"
        except Exception as exc:
            log.info("OpenLibrary comic fallback failed for %r: %s", q, exc)
            return [], "ComicVine and OpenLibrary both failed"
    rows = await _openlibrary_search(session, q, subject="graphic novels")
    return rows, (
        "via OpenLibrary — set COMICVINE_API_KEY for full comic data"
        if rows else "no ComicVine key configured (OpenLibrary had no matches)"
    )


async def _lookup_book(session, q):
    return await _openlibrary_search(session, q), None


# ------------------------------------------------------------- builders -----

def _rating_text(value):
    if value in (None, "", 0):
        return "unrated"
    return f"⭐ {round(value, 1)}/10"


def _card_html(rows, header):
    parts = [f"<h2>{_html.escape(header)}</h2>"]
    for i, row in enumerate(rows, 1):
        image = row.get("image") or ""
        if image.startswith(("http://", "https://")):
            parts.append(f'<img src="{_html.escape(image, quote=True)}"/>')
        link = _html.escape(row["url"], quote=True)
        title = _html.escape(row["title"])
        parts.append(f"<h3>{i}. <a href=\"{link}\">{title}</a></h3>")
        meta = " · ".join(
            part for part in (_rating_text(row.get("rating")),
                              row.get("details") or "") if part
        )
        if meta:
            parts.append(f"<p>{_html.escape(meta)}</p>")
        if row.get("synopsis"):
            parts.append(f"<p>{_html.escape(row['synopsis'])}</p>")
    return "\n".join(parts)


def _fallback_caption(row):
    """Photo caption (or bare text) for the top result in the fallback path."""
    title = _html.escape(row["title"])
    link = _html.escape(row["url"], quote=True)
    lines = [f"<b><a href=\"{link}\">{title}</a></b>"]
    meta = " · ".join(
        part for part in (_rating_text(row.get("rating")),
                          row.get("details") or "") if part
    )
    if meta:
        lines.append(meta)
    if row.get("synopsis"):
        lines.append("")
        lines.append(_html.escape(row["synopsis"]))
    return "\n".join(lines)


async def send_lookup(context, chat_id, thread, rows, header, note=None):
    """One rich message with all cards; photo+caption fallback; then text."""
    if note:
        header = f"{header} ({note})"
    try:
        await context.bot._post(
            "sendRichMessage",
            data={
                "chat_id": chat_id,
                "message_thread_id": thread,
                "rich_message": {"html": _card_html(rows, header)},
            },
        )
        return "rich"
    except Exception as exc:
        log.info("rich lookup failed (%s); using photo/text fallback", exc)

    row = rows[0]
    threaded = thread if thread != 1 else None
    caption = _fallback_caption(row)[:1024]
    try:
        if (row.get("image") or "").startswith(("http://", "https://")):
            await context.bot.send_photo(
                chat_id=chat_id, photo=row["image"], caption=caption,
                parse_mode=ParseMode.HTML, message_thread_id=threaded,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
            return "photo"
    except Exception as exc:
        log.info("cover send failed for %s (%s); using text", row["url"], exc)
    await context.bot.send_message(
        chat_id=chat_id, message_thread_id=threaded, text=caption,
        parse_mode=ParseMode.HTML, link_preview_options=LinkPreviewOptions(is_disabled=True),
    )
    return "text"


# -------------------------------------------------------------- commands ----

_LOOKUPS = {
    "anime": _lookup_anime,
    "eac": _lookup_manga,
    "comic": _lookup_comic,
    "book": _lookup_book,
}

_HEADERS = {
    "anime": "Anime results",
    "eac": "Manga / manhwa / manhua results",
    "comic": "Comic results",
    "book": "Book results",
}

_EXAMPLES = {
    "anime": "chainsaw man episode list",
    "eac": "solo leveling chapters",
    "comic": "batman year one",
    "book": "dune frank herbert",
}


def _make_cmd(name):
    async def cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        msg = update.effective_message
        chat = update.effective_chat
        q = " ".join(ctx.args).strip()
        if not q:
            await msg.reply_text(
                f"🔍 Usage: /{name} <search term>\n"
                f"Example: /{name} {_EXAMPLES[name]}",
            )
            return
        thread_id = get_thread_id(msg)
        async with aiohttp.ClientSession(
            timeout=_HTTP, headers={"User-Agent": _UA}
        ) as session:
            try:
                rows, note = await _LOOKUPS[name](session, q)
            except Exception as exc:
                await msg.reply_text(
                    f"❌ Lookup failed for <b>{_html.escape(q[:120])}</b> — "
                    f"the lookup sources are unreachable "
                    f"({_html.escape(str(exc)[:120])}).",
                    parse_mode=ParseMode.HTML,
                )
                return
        if not rows:
            await msg.reply_text(
                f"🔎 No results for <b>{_html.escape(q[:120])}</b>. Try different "
                f"spelling or a simpler term.",
                parse_mode=ParseMode.HTML,
            )
            return
        header = f"{_HEADERS[name]} — {_html.escape(q[:80])}"
        mode = await send_lookup(ctx, chat.id, thread_id, rows, header, note)
        log.info("lookup %s %r -> %d results (%s)", name, q, len(rows), mode)

    cmd.__name__ = f"cmd_{name}"
    return cmd


cmd_anime = _make_cmd("anime")
cmd_eac = _make_cmd("eac")
cmd_comic = _make_cmd("comic")
cmd_book = _make_cmd("book")

# All member-facing: group-locked (same group only), NOT admin-gated.
for _name in ("cmd_anime", "cmd_eac", "cmd_comic", "cmd_book"):
    globals()[_name] = group_command()(globals()[_name])


def build_handlers():
    handlers = []
    for name in ("anime", "eac", "comic", "book"):
        handlers.append(CommandHandler(name, globals()[f"cmd_{name}"]))
    # plural aliases the admins also use (/books, /comics)
    handlers.append(CommandHandler("books", cmd_book))
    handlers.append(CommandHandler("comics", cmd_comic))
    return handlers
