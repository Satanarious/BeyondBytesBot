"""Media & book lookups for BBbot.

Commands — for ALL members (group-locked, NOT admin-only):
  /anime <q>            — MAL (Jikan) → AniList GraphQL fallback
  /eac <q>              — manga/manhwa/manhua: MAL → AniList fallback
  /books /book <q>      — OpenLibrary
  /comics /comic <q>    — ComicVine (COMICVINE_API_KEY) → OpenLibrary fallback

Flow: the command shows a TAPPABLE suggestion list (inline keyboard, paged).
Tapping a suggestion deletes the list and posts a detailed rich-message card:
cover art inline, title-embedded link, score/format/counts/genres meta lines,
synopsis in an expandable quote, and a footer link to the source page.
Note: Jikan/MAL is unreachable from some datacenter hosts (Oracle Cloud) — the
AniList fallback then serves results automatically and is labelled as such.
"""

import html as _html
import logging
import os
import re
import time
import uuid

import aiohttp
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import CommandHandler, ContextTypes

from guard import group_command, is_meant_for
from utils import get_thread_id

log = logging.getLogger("bbbot.media")

_HTTP = aiohttp.ClientTimeout(total=20, connect=6)
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 BBbot/1.0")
_OL_HEADERS = {"User-Agent": _UA}     # OpenLibrary rejects bare bot UAs
_SYN_MAX = 480

_ANILIST_URL = "https://graphql.anilist.co"
_JIKAN_URL = "https://api.jikan.moe/v4"
_OL_URL = "https://openlibrary.org"
_CV_URL = "https://comicvine.gamespot.com/api"

_PAGE_SIZE = 5
_SUGGEST_TTL = 15 * 60                # seconds — tap-able list lifetime

_ANILIST_QUERY = """
query ($q: String!, $type: MediaType, $n: Int) {
  Page(perPage: $n) {
    media(search: $q, type: $type, sort: SEARCH_MATCH) {
      title { english romaji native }
      averageScore
      episodes
      chapters
      volumes
      format
      status
      duration
      seasonYear
      genres
      description
      siteUrl
      coverImage { large }
    }
  }
}
"""


def _strip_html(value):
    text = re.sub(r"<[^>]+>", " ", str(value or ""))
    return " ".join(_html.unescape(text).split())


def _synopsis(value, limit=_SYN_MAX):
    text = _strip_html(value)
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "…"
    return text


def _abs_url(url):
    url = str(url or "").strip()
    return url if url.startswith(("http://", "https://")) else ""


async def _get_json(session, url, params=None, json_body=None, headers=None):
    method = "POST" if json_body is not None else "GET"
    async with session.request(
        method, url, params=params, json=json_body, headers=headers
    ) as resp:
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status} from the lookup source")
        return await resp.json(content_type=None)


# ------------------------------------------------------------ Jikan / MAL ---

def _count_items(kind, item):
    counts = []
    if kind == "anime" and item.get("episodes"):
        counts.append(f"{item['episodes']} episodes")
    if kind == "manga":
        if item.get("chapters"):
            counts.append(f"{item['chapters']} chapters")
        if item.get("volumes"):
            counts.append(f"{item['volumes']} volumes")
    return counts


async def _jikan_search(session, kind, q, limit=8):
    data = await _get_json(
        session, f"{_JIKAN_URL}/{kind}",
        params={"q": q, "limit": limit},
    )
    rows = []
    for item in (data.get("data") or [])[:limit]:
        items = []
        items.extend(_count_items(kind, item))
        if item.get("type"):
            items.append(f"Type: {item['type']}")
        if item.get("status"):
            items.append(f"Status: {item['status']}")
        if item.get("duration") and kind == "anime":
            items.append(f"Duration: {item['duration']}")
        aired = item.get("aired") or item.get("published") or {}
        if (aired.get("string") or "").strip():
            items.append(f"Aired: {aired['string']}")
        if item.get("members"):
            items.append(f"Members: {item['members']:,}")
        genres = [g.get("name") for g in (item.get("genres") or []) if g.get("name")]
        if genres:
            items.append("Genres: " + ", ".join(genres[:6]))
        image = ((item.get("images") or {}).get("jpg") or {}).get("large_image_url", "")
        rows.append({
            "title": item.get("title") or "Unknown title",
            "url": item.get("url") or f"https://myanimelist.net/{kind}/",
            "image": _abs_url(image),
            "score": item.get("score"),
            "score_scale": 10,
            "source": "MyAnimeList",
            "source_url": "MyAnimeList",
            "button_line": " · ".join(_count_items(kind, item)) or item.get("type") or "",
            "details": items,
            "synopsis": _strip_html(item.get("synopsis")),
        })
    return rows


# ------------------------------------------------------------- AniList ------

def _anilist_rows(data, limit=8, seen=None):
    """normalize AniList media into the shared row shape."""
    rows = []
    for item in (((data.get("data") or {}).get("Page") or {}).get("media") or [])[:limit]:
        title = item.get("title") or {}
        name = (title.get("english") or title.get("romaji")
                or title.get("native") or "Unknown title")
        items = []
        if item.get("episodes"):
            items.append(f"{item['episodes']} episodes")
        if item.get("chapters"):
            items.append(f"{item['chapters']} chapters")
        if item.get("volumes"):
            items.append(f"{item['volumes']} volumes")
        if item.get("format"):
            items.append(f"Format: {item['format']}")
        if item.get("status"):
            items.append(f"Status: {item['status']}")
        if item.get("duration"):
            items.append(f"Duration: {item['duration']} min")
        if item.get("seasonYear"):
            items.append(f"Year: {item['seasonYear']}")
        genres = item.get("genres") or []
        if genres:
            items.append("Genres: " + ", ".join(map(str, genres[:6])))
        rows.append({
            "title": name,
            "url": item.get("siteUrl") or "https://anilist.co",
            "image": _abs_url((item.get("coverImage") or {}).get("large")),
            "score": item.get("averageScore"),
            "score_scale": 100,
            "source": "AniList",
            "source_url": "AniList",
            "button_line": " · ".join(
                part for part in (
                    f"{item['episodes']} ep" if item.get("episodes") else "",
                    f"{item['chapters']} ch" if item.get("chapters") else "",
                    item.get("format") or "",
                ) if part
            ),
            "details": items,
            "synopsis": _strip_html(item.get("description")),
        })
    return rows


async def _anilist_search(session, q, media_type, limit=8):
    data = await _get_json(
        session, _ANILIST_URL,
        json_body={
            "query": _ANILIST_QUERY,
            "variables": {"q": q, "type": media_type, "n": limit},
        },
    )
    return _anilist_rows(data, limit=limit)


# ----------------------------------------------------------- ComicVine ------

async def _comicvine_search(session, q, api_key, limit=8):
    data = await _get_json(
        session, f"{_CV_URL}/search/",
        params={
            "api_key": api_key,
            "format": "json",
            "query": q,
            "limit": limit,
            "resources": "volume",
            "field_list":
                "name,deck,description,image,count_of_issues,start_year,"
                "publisher,site_detail_url",
        },
    )
    rows = []
    for item in (data.get("results") or [])[:limit]:
        image = (item.get("image") or {}).get("medium_url") or \
                (item.get("image") or {}).get("thumb_url") or ""
        publisher = (item.get("publisher") or {}).get("name") or ""
        items = []
        if item.get("count_of_issues"):
            items.append(f"Issues: {item['count_of_issues']}")
        if item.get("start_year"):
            items.append(f"First published: {item['start_year']}")
        if publisher:
            items.append(f"Publisher: {publisher}")
        rows.append({
            "title": _strip_html(item.get("name")) or "Unknown series",
            "url": item.get("site_detail_url") or "https://comicvine.gamespot.com",
            "image": _abs_url(image),
            "score": None,           # ComicVine volumes carry no simple rating
            "score_scale": 10,
            "source": "ComicVine",
            "source_url": "ComicVine",
            "button_line": " · ".join(
                part for part in (
                    f"{item['count_of_issues']} issues" if item.get("count_of_issues") else "",
                    publisher or "",
                ) if part
            ),
            "details": items,
            "synopsis": _strip_html(item.get("deck") or item.get("description")),
        })
    return rows


# ------------------------------------------------------------ OpenLibrary ---

async def _openlibrary_search(session, q, subject=None, limit=8):
    params = {
        "q": q, "limit": limit,
        "fields": ",".join((
            "key", "title", "subtitle", "author_name", "first_publish_year",
            "edition_count", "cover_i", "ratings_average", "ratings_count",
            "subject", "first_sentence", "ia",
        )),
    }
    if subject:
        params["subject"] = subject
    data = await _get_json(session, f"{_OL_URL}/search.json",
                           params=params, headers=_OL_HEADERS)
    rows = []
    for item in (data.get("docs") or [])[:limit]:
        work_key = item.get("key") or ""
        url = f"https://openlibrary.org{work_key}" if work_key else _OL_URL
        authors = ", ".join((item.get("author_name") or [])[:4])
        subjects = [s for s in (item.get("subject") or [])
                    if isinstance(s, str)][:6]
        items = []
        if authors:
            items.append(f"By: {authors}")
        if item.get("first_publish_year"):
            items.append(f"First published: {item['first_publish_year']}")
        if item.get("edition_count"):
            items.append(f"Editions: {item['edition_count']}")
        if item.get("ratings_count"):
            items.append(f"Ratings: {item['ratings_count']:,}")
        if subjects:
            items.append("Subjects: " + ", ".join(subjects))
        canvas = ""
        if item.get("ia"):
            # Internet Archive full-text copies often render a browsable cover
            canvas = f"https://archive.org/details/{item['ia'][0]}"
        first = _strip_html(
            item.get("first_sentence") if isinstance(item.get("first_sentence"), str)
            else ((item.get("first_sentence") or {}).get("value") if isinstance(item.get("first_sentence"), dict) else "")
        )
        rows.append({
            "title": (item.get("title") or "Unknown title").strip(),
            "url": canvas or url,
            "image": (f"https://covers.openlibrary.org/b/id/{item['cover_i']}-L.jpg"
                      if item.get("cover_i") else ""),
            "score": item.get("ratings_average"),
            "score_scale": 5,
            "source": "OpenLibrary",
            "source_url": "OpenLibrary",
            "button_line": (f"by {authors}" if authors
                            else str(item.get("first_publish_year") or "")),
            "details": items,
            "synopsis": first,
            "work_key": work_key,
        })
    return rows


async def _ol_enrich(row):
    """Lazy detail fetch for OpenLibrary rows: description from the work
    document and rating/counts from the ratings endpoint (search results
    frequently omit both). Mutates + returns the row."""
    key = row.get("work_key")
    if not key:
        return row
    timeout = aiohttp.ClientTimeout(total=10, connect=5)
    async with aiohttp.ClientSession(timeout=timeout, headers=_OL_HEADERS) as session:
        if not (row.get("synopsis") or "").strip():
            try:
                work = await _get_json(session, f"{_OL_URL}{key}.json")
                desc = work.get("description")
                if isinstance(desc, dict):
                    desc = desc.get("value")
                row["synopsis"] = _strip_html(str(desc if isinstance(desc, str) else "")) \
                    if desc else ""
                if row.get("synopsis"):
                    log.info("ol enrich: description for %s (%d chars)",
                             key, len(row["synopsis"]))
            except Exception as exc:
                log.info("ol enrich: no description for %s (%s)", key, exc)
        if row.get("score") in (None, "", 0):
            try:
                ratings = await _get_json(session, f"{_OL_URL}{key}/ratings.json")
                summary = ratings.get("summary") or {}
                avg = summary.get("average")
                if avg:
                    row["score"] = avg
                    row["score_scale"] = 5
                    if summary.get("count") and not any(
                        str(x).lower().startswith("ratings")
                        for x in (row.get("details") or [])
                    ):
                        row["details"] = list(row.get("details") or []) + [
                            f"Ratings: {summary['count']:,}",
                        ]
            except Exception as exc:
                log.info("ol enrich: no ratings for %s (%s)", key, exc)
    return row


# ---------------------------------------------------------------- TMDB -------

_TMDB_URL = "https://api.themoviedb.org/3"
_TMDB_IMG = "https://image.tmdb.org/t/p/w500"


def _human_duration(minutes):
    try:
        total = int(str(minutes).split()[0])
    except (ValueError, IndexError):
        return ""
    h, m = total // 60, total % 60
    if h and m:
        return f"{h}h {m}m"
    if h:
        return f"{h}h"
    return f"{m}m"


def _tmdb_key():
    """v3 API key (or a v4 read-access token works with ?api_key= too)."""
    return (os.getenv("TMDB_API_KEY") or os.getenv("TMDB_KEY") or "").strip()


async def _tmdb_search(session, q, limit=8):
    """Search movies + TV via TMDB /search/multi. Person entries are skipped."""
    api_key = _tmdb_key()
    if not api_key:
        raise RuntimeError(
            "TMDB_API_KEY missing in the bot .env (free key: "
            "themoviedb.org -> settings -> API)"
        )
    data = await _get_json(
        session, f"{_TMDB_URL}/search/multi",
        params={"api_key": api_key, "query": q, "page": 1,
                "include_adult": "false", "language": "en-US"},
    )
    rows = []
    for item in (data.get("results") or []):
        kind = item.get("media_type")
        if kind not in ("movie", "tv"):
            continue                     # persons/companies are not 'titles'
        ttype = "movie" if kind == "movie" else "tv"
        title = _strip_html(item.get("title") or item.get("name")) or "Unknown title"
        date = item.get("release_date") or item.get("first_air_date") or ""
        year = date[:4] if date else ""
        score = item.get("vote_average") or None
        votes = item.get("vote_count") or 0
        poster = item.get("poster_path") or item.get("backdrop_path") or ""
        items = [
            f"Type: {'Movie' if ttype == 'movie' else 'TV Series'}",
        ]
        if year:
            items.append(f"Year: {year}")
        if votes:
            items.append(f"Votes: {votes:,}")
        rows.append({
            "title": title,
            "url": f"https://www.themoviedb.org/{ttype}/{item.get('id')}",
            "image": _abs_url(f"{_TMDB_IMG}{poster}") if poster else "",
            "score": score,
            "score_scale": 10,
            "source": "TMDB",
            "source_url": "TMDB",
            "button_line": " · ".join(
                part for part in (
                    year,
                    _score_text({"score": score, "score_scale": 10}, short=True) or "",
                ) if part
            ),
            "details": items,
            # /search/multi carries a short overview already — cards render
            # even if enrichment fails; tap pulls runtime/genres/cast too.
            "synopsis": _synopsis(item.get("overview"), limit=700),
            "tmdb_type": ttype,
            "tmdb_id": str(item.get("id") or ""),
        })
        if len(rows) >= limit:
            break
    return rows


async def _lookup_tmdb(session, q):
    return await _tmdb_search(session, q), None


async def _tmdb_enrich(row):
    """Fetch runtime/genres/cast/credits for the tapped card from the title
    endpoint. The card already renders with search-level data if this fails."""
    ttype, tmdb_id = row.get("tmdb_type"), row.get("tmdb_id")
    api_key = _tmdb_key()
    if not ttype or not tmdb_id or not api_key:
        return row
    timeout = aiohttp.ClientTimeout(total=10, connect=5)
    async with aiohttp.ClientSession(
        timeout=timeout, headers={"User-Agent": _UA}
    ) as session:
        try:
            data = await _get_json(
                session, f"{_TMDB_URL}/{ttype}/{tmdb_id}",
                params={"api_key": api_key, "language": "en-US",
                        "append_to_response": "credits"},
            )
        except Exception as exc:
            log.info("tmdb enrich failed for %s/%s (%s)", ttype, tmdb_id, exc)
            return row

    items = []
    if ttype == "movie":
        runtime = _human_duration(data.get("runtime"))
        if runtime:
            items.append(f"Runtime: {runtime}")
    else:
        seasons = data.get("number_of_seasons")
        episodes = data.get("number_of_episodes")
        if seasons:
            tail = f" · {episodes} ep" if episodes else ""
            items.append(f"Seasons: {seasons}{tail}")
        ep_runtime = (data.get("episode_run_time") or [])
        if ep_runtime and ep_runtime[0]:
            items.append(f"Episode: {_human_duration(ep_runtime[0])}")

    cast = ((data.get("credits") or {}).get("cast") or [])[:5]
    names = [c.get("name") for c in cast if c.get("name")]
    if names:
        items.append("Starring: " + ", ".join(names))

    crew = ((data.get("credits") or {}).get("crew") or [])
    if ttype == "movie":
        makers = [c.get("name") for c in crew if c.get("job") == "Director"
                  and c.get("name")]
        if makers:
            items.append("Director: " + ", ".join(dict.fromkeys(makers)))
    else:
        makers = [c.get("name") for c in (data.get("created_by") or [])[:3]
                  if c.get("name")]
        if makers:
            items.append("Creator: " + ", ".join(makers))

    votes = data.get("vote_count")
    if votes:
        items.append(f"Votes: {votes:,}")

    genres = [g.get("name") for g in (data.get("genres") or []) if g.get("name")]
    if genres:
        items.append("Genres: " + ", ".join(genres[:6]))

    row["details"] = items
    score = data.get("vote_average")
    if score:
        row["score"] = score
        row["score_scale"] = 10
    return row


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


async def _lookup_comic(session, q):
    """ComicVine when configured, OpenLibrary graphic novels otherwise."""
    api_key = (os.getenv("COMICVINE_API_KEY") or "").strip()
    ol_note = None
    if api_key:
        try:
            rows = await _comicvine_search(session, q, api_key)
            if rows:
                return rows, None
            log.info("ComicVine: no results for %r; trying OpenLibrary", q)
            ol_note = "ComicVine had no matches — via OpenLibrary"
        except Exception as exc:
            log.info("ComicVine failed for %r (%s); trying OpenLibrary", q, exc)
            ol_note = "via OpenLibrary (ComicVine error)"
    else:
        ol_note = ("via OpenLibrary — add COMICVINE_API_KEY for full comic data")
    try:
        rows = await _openlibrary_search(session, q, subject="comics")
    except Exception:
        rows = await _openlibrary_search(session, q, subject="graphic novels")
    return rows, ol_note


async def _lookup_book(session, q):
    return await _openlibrary_search(session, q), None


# ----------------------------------------------------- suggestion UX --------

_SUGGEST_CACHE = {}          # qid -> {"rows", "source", "thread", "note", "expires"}


def _store_suggest(source, rows, thread, note):
    qid = uuid.uuid4().hex[:10]
    _SUGGEST_CACHE[qid] = {
        "rows": rows,
        "source": source,
        "thread": thread,
        "note": note,
        "expires": time.time() + _SUGGEST_TTL,
    }
    # opportunistic sweep so the cache cannot grow unbounded
    now = time.time()
    for old in [k for k, v in _SUGGEST_CACHE.items() if v["expires"] < now]:
        _SUGGEST_CACHE.pop(old, None)
    return qid


def _score_text(row, short=False):
    score, scale = row.get("score"), row.get("score_scale")
    if score in (None, "", 0):
        return None
    decimals = 1 if scale in (5, 10) else 0
    value = f"{round(score, decimals)}"
    if short:
        return f"⭐{value}" if scale != 5 else f"⭐{value}"
    unit = {"10": "/10", "100": "/100", "5": "/5"}.get(str(scale), "")
    return f"⭐ {value}{unit}"


def _suggests_keyboard(source, qid, rows, page=0):
    pages = max(1, (len(rows) + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    chunk = rows[page * _PAGE_SIZE:(page + 1) * _PAGE_SIZE]
    kb_rows = []
    for idx, row in enumerate(chunk, start=page * _PAGE_SIZE):
        score = _score_text(row, short=True) or ""
        tag = " ".join(part for part in (score, str(row.get("button_line") or "")[:70]) if part)
        label = str(row["title"])[:110]
        if tag and len(label) + len(tag) + 3 < 190:
            label = f"{label} — {tag}"
        kb_rows.append([InlineKeyboardButton(
            label[:128], callback_data=f"lk:{source}:{qid}:{idx}",
        )])
    if pages > 1:
        nav = []
        nav.append(InlineKeyboardButton("◀", callback_data=f"lkp:{source}:{qid}:{page - 1}"))
        nav.append(InlineKeyboardButton(
            f"{page + 1}/{pages}", callback_data=f"lk0:{source}:{qid}:{page}"))
        nav.append(InlineKeyboardButton("▶", callback_data=f"lkp:{source}:{qid}:{page + 1}"))
        kb_rows.append(nav)
    kb_rows.append([InlineKeyboardButton("✖ Close", callback_data=f"lkx:x:{qid}:0")])
    return InlineKeyboardMarkup(kb_rows)


def _detail_html(row):
    image = _abs_url(row.get("image"))
    title = _html.escape(row["title"])
    link = _html.escape(row["url"], quote=True)
    parts = []
    if image:
        parts.append(f'<img src="{_html.escape(image, quote=True)}"/>')
    parts.append(f'<h2><a href="{link}">{title}</a></h2>')

    score = _score_text(row)
    meta_bits = []
    for line in row.get("details") or []:
        line = str(line).strip()
        if line and not line.lower().startswith("genres"):
            meta_bits.append(_html.escape(line))
    meta = " · ".join([b for b in ([_html.escape(score)] if score else []) + meta_bits if b])
    if meta:
        parts.append(f"<p>{meta}</p>")

    genres = [_html.escape(str(x).strip()) for x in (row.get("details") or [])
              if str(x).lower().startswith("genres")]
    for genre_line in genres:
        parts.append(f"<p>{genre_line}</p>")

    synopsis = (row.get("synopsis") or "").strip()
    if synopsis:
        parts.append(
            "<blockquote expandable>"
            f"{_html.escape(_synopsis(synopsis, limit=900) or '')}"
            "</blockquote>"
        )
    source = _html.escape(row.get("source") or "source")
    parts.append(
        f'<p><a href="{link}">Open on {source} ↗</a> '
        f"— tap a new /anime /eac /book /comic search any time</p>"
    )
    return "\n".join(parts)


def _detail_caption(row):
    title = _html.escape(row["title"])
    link = _html.escape(row["url"], quote=True)
    lines = [f"<b><a href=\"{link}\">{title}</a></b>"]
    score = _score_text(row)
    meta = " · ".join(
        part for part in ([score] if score else []) +
        [str(x) for x in (row.get("details") or []) if x]
    )
    if meta:
        lines.append(_html.escape(meta))
    if row.get("synopsis"):
        lines.append("")
        lines.append(_html.escape(_synopsis(row["synopsis"])))
    return "\n".join(lines) or title


async def _send_detail(context, chat_id, thread, row):
    """Detail card ladder: rich → photo+caption → plain text."""
    threaded = thread if thread and thread != 1 else None
    html = _detail_html(row)
    try:
        await context.bot._post(
            "sendRichMessage",
            data={
                "chat_id": chat_id,
                "message_thread_id": thread,
                "rich_message": {"html": html},
            },
        )
        return "rich"
    except Exception as exc:
        log.info("rich detail failed (%s); using photo/text fallback", exc)

    caption = _detail_caption(row)[:1024]
    image = _abs_url(row.get("image"))
    try:
        if image:
            await context.bot.send_photo(
                chat_id=chat_id, photo=image, caption=caption,
                parse_mode=ParseMode.HTML, message_thread_id=threaded,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
            return "photo"
    except Exception as exc:
        log.info("cover send failed for %s (%s); using text", image, exc)
    await context.bot.send_message(
        chat_id=chat_id, message_thread_id=threaded, text=caption,
        parse_mode=ParseMode.HTML,
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )
    return "text"


async def on_look_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """All suggestion-list taps: lk (select), lkp (page), lkx (close)."""
    query = update.callback_query
    if query is None:
        return
    data = query.data or ""
    try:
        await query.answer()
    except Exception:
        pass

    if not is_meant_for(update.effective_chat.id):
        try:
            await query.answer("Not meant for this group.", show_alert=True)
        except Exception:
            pass
        return

    parts = (data.split(":") + ["0", "0", "0"])[:4]
    kind, _source, qid, raw_idx = parts
    entry = _SUGGEST_CACHE.get(qid)

    if kind == "lk0" or not entry:
        if entry is None and kind in ("lk", "lkp"):
            try:
                await query.answer("This list expired — run the search again.",
                                   show_alert=True)
            except Exception:
                pass
        return

    if entry["expires"] < time.time():
        _SUGGEST_CACHE.pop(qid, None)
        try:
            await query.answer("This list expired — run the search again.",
                               show_alert=True)
        except Exception:
            pass
        return

    source = entry["source"]
    rows = entry["rows"]

    if kind == "lkx":
        _SUGGEST_CACHE.pop(qid, None)
        try:
            await query.message.delete()
        except Exception:
            pass
        return

    if kind == "lkp":
        try:
            page = int(raw_idx)
        except ValueError:
            page = 0
        pages = max(1, (len(rows) + _PAGE_SIZE - 1) // _PAGE_SIZE)
        page = max(0, min(page, pages - 1))
        note = entry.get("note")
        header = _HEADERS[source] + (" — " + note if note else "")
        try:
            await query.message.edit_text(
                header,
                parse_mode=ParseMode.HTML,
                reply_markup=_suggests_keyboard(source, qid, rows, page),
            )
        except Exception:
            pass                     # same page re-tap → Telegram "not modified"
        return

    if kind == "lk":
        try:
            idx = int(raw_idx)
        except ValueError:
            return
        if not 0 <= idx < len(rows):
            try:
                await query.answer("That entry is no longer available.",
                                   show_alert=True)
            except Exception:
                pass
            return
        row = rows[idx]
        thread = entry["thread"]
        _SUGGEST_CACHE.pop(qid, None)
        # OpenLibrary rows lack description/ratings in search output — fetch
        # them now so the detail card is full.
        if row.get("work_key"):
            try:
                row = await _ol_enrich(row)
            except Exception as exc:
                log.info("ol enrich failed on tap (%s)", exc)
        if row.get("tmdb_id"):
            try:
                row = await _tmdb_enrich(row)
            except Exception as exc:
                log.info("tmdb enrich failed on tap (%s)", exc)
        try:
            await query.message.delete()    # list goes away once a pick is made
        except Exception:
            pass
        mode = await _send_detail(context, update.effective_chat.id, thread, row)
        log.info("lookup detail %s idx=%d -> %s", source, idx, mode)


# -------------------------------------------------------------- commands ----

_LOOKUPS = {
    "anime": _lookup_anime,
    "eac": _lookup_manga,
    "comic": _lookup_comic,
    "book": _lookup_book,
    "tmdb": _lookup_tmdb,
}

_HEADERS = {
    "anime": "🎬 Anime results",
    "eac": "📚 Manga / manhwa / manhua results",
    "comic": "💥 Comic results",
    "book": "📖 Book results",
    "tmdb": "🍿 Movies & TV results",
}

_EXAMPLES = {
    "anime": "chainsaw man",
    "eac": "solo leveling",
    "comic": "batman year one",
    "book": "dune frank herbert",
    "tmdb": "the dark knight",
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
                log.info("lookup %s %r failed: %s", name, q, exc)
                await msg.reply_text(
                    f"❌ Lookup failed for <b>{_html.escape(q[:120])}</b> — "
                    f"the lookup sources are unreachable "
                    f"({_html.escape(str(exc)[:120])}). Try again in a moment.",
                    parse_mode=ParseMode.HTML,
                )
                return
        if not rows:
            await msg.reply_text(
                f"🔎 No results for <b>{_html.escape(q[:120])}</b>. "
                f"Try a different spelling or a simpler term.",
                parse_mode=ParseMode.HTML,
            )
            return
        qid = _store_suggest(name, rows, thread_id, note)
        header = _HEADERS[name] + (" — " + note if note else "")
        header += f"\n🔎 <b>{_html.escape(q[:80])}</b> — tap a result for full details:"
        try:
            await ctx.bot.send_message(
                chat_id=chat.id,
                message_thread_id=(thread_id if thread_id != 1 else None),
                text=header,
                parse_mode=ParseMode.HTML,
                reply_markup=_suggests_keyboard(name, qid, rows, 0),
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
            log.info("lookup %s %r -> %d suggestions (paged)", name, q, len(rows))
        except Exception as exc:
            log.info("suggestion list send failed (%s); direct card", exc)
            await _send_detail(ctx, chat.id, thread_id, rows[0])

    cmd.__name__ = f"cmd_{name}"
    return cmd


cmd_anime = group_command()(_make_cmd("anime"))
cmd_eac = group_command()(_make_cmd("eac"))
cmd_comic = group_command()(_make_cmd("comic"))
cmd_book = group_command()(_make_cmd("book"))
cmd_tmdb = group_command()(_make_cmd("tmdb"))


def build_handlers():
    """Member-facing lookup handlers (group-locked, NOT admin-only)."""
    handlers = [
        CommandHandler("anime", cmd_anime),
        CommandHandler("eac", cmd_eac),
        CommandHandler("comic", cmd_comic),
        CommandHandler("book", cmd_book),
        CommandHandler("books", cmd_book),      # plural alias
        CommandHandler("comics", cmd_comic),    # plural alias
        CommandHandler("tmdb", cmd_tmdb),
        CommandHandler("movies", cmd_tmdb),     # alias
        CommandHandler("movie", cmd_tmdb),      # alias
    ]
    return handlers
