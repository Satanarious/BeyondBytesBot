"""Daily AI digest for BBbot.

Articles discovered by the RSS poller are queued per topic (pending_articles).
Once a day, at the configured UTC time, every topic with a queue gets one
message: Gemini's pick of up to 5 top stories, each with the article link
embedded. The queued links are deleted right after posting.
"""

import asyncio
import html
import json
import logging
import os
import re
from datetime import datetime, time, timezone
from urllib.parse import urljoin

import aiohttp
from telegram import InputMediaPhoto, LinkPreviewOptions, Update
from telegram.constants import ParseMode
from telegram.ext import CommandHandler, ContextTypes

import db
from guard import admin_command, is_meant_for
from utils import get_thread_id

log = logging.getLogger("bbbot.digest")

JOB_NAME = "digest_daily"
DEFAULT_TIME = "18:00"          # UTC, 24h format
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_TIMEOUT = aiohttp.ClientTimeout(total=180)
_RETRYABLE_STATUS = {429, 500, 503}
# tried in order when the primary model is dead (404) or out of quota (429)
FALLBACK_MODELS = ("gemini-3.1-flash-lite", "gemini-2.5-flash-lite")


def _int_env(name, default, lo, hi):
    try:
        return max(lo, min(int(os.getenv(name) or default), hi))
    except ValueError:
        return default


# seconds to wait between processing one topic's digest and the next
DIGEST_GAP = _int_env("DIGEST_GAP_SECONDS", 90, 10, 900)

_GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

PROMPT_TEMPLATE = (
    "You are the daily news editor for a Telegram group topic. Below is a list of "
    "article headlines collected since yesterday from this topic's RSS feeds.\n\n"
    "Task: pick the up to 5 most relevant and interesting stories and write a short "
    "summary for each.\n"
    "Rules:\n"
    "- If fewer than 5 articles are provided, summarize all of them.\n"
    "- Each summary is 1\u20132 sentences, factual, no hype, in English.\n"
    "- Use ONLY the URLs given below; do not invent, alter, or shorten any URL.\n"
    "- Do not add any commentary outside the JSON.\n\n"
    "ARTICLES:\n"
    "{lines}\n\n"
    'Respond ONLY with JSON of the form {{"items": [{{"title": "...", "summary": "...", "url": "..."}}]}}'
)


# ----------------------------------------------------------- schedule ------

def get_digest_time(default=DEFAULT_TIME):
    """Persisted posting time as 'HH:MM' (UTC). Falls back on anything weird."""
    raw = str(db.get_setting("digest_time_utc", default))
    match = _TIME_RE.match(raw.strip())
    if not match:
        return default
    hh, mm = int(match.group(1)), int(match.group(2))
    return f"{hh:02d}:{mm:02d}"


def reschedule_digest(job_queue, hhmm=None):
    """(Re)create the daily digest job at the given/ persisted time."""
    hhmm = hhmm or get_digest_time()
    hh, mm = (int(part) for part in hhmm.split(":"))
    for job in job_queue.jobs():
        if job.name == JOB_NAME:
            job.schedule_removal()
    job_queue.run_daily(
        process_all,
        time=time(hh, mm, tzinfo=timezone.utc),
        name=JOB_NAME,
        data=None,
    )
    return hhmm


# ------------------------------------------------------------- posting -----

def _topic_label(titles, thread_id):
    if thread_id == 1:
        return titles.get(1) or "General"
    return titles.get(thread_id) or f"Topic #{thread_id}"


async def process_all(context: ContextTypes.DEFAULT_TYPE):
    """Daily job: one digest per topic that collected articles since yesterday.

    Topics are processed one at a time with a gap (DIGEST_GAP_SECONDS) so the
    Gemini free-tier rate limits aren't hit by back-to-back requests.
    """
    topics = [t for t in db.pending_topics() if is_meant_for(t["chat_id"])]
    for index, topic in enumerate(topics):
        if index > 0:
            await asyncio.sleep(DIGEST_GAP)
        try:
            await process_topic(context, topic["chat_id"], topic["thread_id"])
        except Exception:
            log.exception("digest failed for chat %s topic %s",
                          topic["chat_id"], topic["thread_id"])


async def process_topic(context, chat_id, thread_id):
    items = db.get_pending(chat_id, thread_id)
    if not items:
        return
    titles = {t["thread_id"]: t["title"] for t in db.topics_for_chat(chat_id)}
    label = _topic_label(titles, thread_id)
    send_thread = thread_id if thread_id != 1 else None
    images = {row["link"]: (row.get("image_url") or "").strip() for row in items}
    n_images = sum(1 for img in images.values() if img)
    model = os.getenv("GEMINI_MODEL") or "gemini-3.5-flash-lite"

    log.info("digest: %d collected for '%s' (%d with image) (model %s)",
             len(items), label, n_images, model)
    try:
        picks = await summarize_with_gemini(items)
        rows = [{"title": p["title"], "summary": p["summary"], "url": p["url"],
                 "image": images.get(p["url"], "")} for p in picks]
        note = None
    except Exception as exc:
        log.warning("gemini digest failed for topic %s: %s", label, exc)
        rows = [{"title": row["title"] or "Untitled", "summary": "",
                 "url": row["link"], "image": images.get(row["link"], "")}
                for row in items[:5]]
        note = "⚠️ AI summary unavailable today"

    await _enrich_images(rows, label)

    posted = False
    try:
        await _send_rich(context, chat_id, send_thread, rows, label, note)
        posted = True
    except Exception as exc:
        log.warning("rich message failed (%s); falling back to album/text", exc)
    if not posted:
        await _post_digest(context, chat_id, send_thread, rows, label, note)
    db.clear_pending(chat_id, thread_id)
    log.info("digest posted via %s for chat %s topic %s (%d collected)",
             "sendRichMessage" if posted else "album/fallback",
             chat_id, thread_id, len(items))


# --------------------------------------------------------------- gemini -----

async def summarize_with_gemini(items):
    """Send headlines to Gemini (JSON mode) and validate the picks.

    Returns list of dicts with keys title/summary/url; urls guaranteed to be
    from the provided set, so the model can't invent links.
    """
    api_key = (os.getenv("GEMINI_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set in /opt/bbbot/.env")

    whitelist = {row["link"] for row in items}
    lines = "\n".join(f"- {row['title']} — {row['link']}" for row in items)
    prompt = PROMPT_TEMPLATE.format(lines=lines)

    # Model chain: configured/default first, then automatic fallbacks.
    # Default is a Flash-Lite: on the free tier those get ~500 RPD vs ~20 RPD
    # for Flash and no free quota for Pro-class/preview models. If one model
    # is retired (404) or its quota is exhausted (429), the next is tried.
    primary = (os.getenv("GEMINI_MODEL") or "gemini-3.5-flash-lite").strip()
    models = [primary]
    for fallback in FALLBACK_MODELS:
        if fallback and fallback not in models:
            models.append(fallback)

    data = None
    last_error = None
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
        for model in models:
            # build payload per model: thinking rules differ between 2.5 and 3.x
            gen_config = {
                "temperature": 0.3,
                "maxOutputTokens": 4096,
                "responseMimeType": "application/json",
            }
            if model.startswith("gemini-2.5"):
                # Only legacy 2.5 flash/lite models support disabling thinking
                # via thinkingBudget. Gemini 3.x removed that parameter (they
                # use thinkingLevel) and reject thinkingBudget with HTTP 400.
                gen_config["thinkingConfig"] = {"thinkingBudget": 0}
            model_payload = {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": gen_config,
            }
            status = None
            try:
                for attempt in (1, 2, 3):
                    async with session.post(
                        _GEMINI_URL.format(model=model),
                        json=model_payload,
                        headers={"x-goog-api-key": api_key},
                    ) as resp:
                        status = resp.status
                        if status == 200:
                            data = await resp.json()
                            break
                        if status in _RETRYABLE_STATUS and attempt < 3:
                            wait = 30 * attempt
                            log.warning("gemini %s HTTP %s (attempt %d/3); "
                                        "retrying in %ss", model, status, attempt, wait)
                        elif status in _RETRYABLE_STATUS:
                            body = (await resp.text())[:200]
                            last_error = (model, status, body)
                            log.warning("gemini %s exhausted retries (HTTP %s)",
                                        model, status)
                            break
                        else:
                            body = (await resp.text())[:300]
                            if status in (400, 401, 403):
                                # key/config problems never improve by switching
                                raise RuntimeError(f"gemini HTTP {status}: {body}")
                            last_error = (model, status, body)
                            log.warning("gemini %s HTTP %s; trying next model",
                                        model, status)
                            break
                    await asyncio.sleep(wait)
                else:
                    last_error = (model, status, "no response")
                    log.warning("gemini %s: request loop exhausted; trying next model", model)
            except RuntimeError:
                raise
            except Exception as exc:
                last_error = (model, None, str(exc))
                log.warning("gemini %s network error: %s", model, exc)

            if data is not None:
                break

    if data is None:
        code = last_error[1] if last_error else "?"
        detail = (last_error[2][:200] if last_error and last_error[2] else "")
        raise RuntimeError(
            f"gemini unusable across models {', '.join(models)} "
            f"(last HTTP {code}): {detail}"
        )

    candidates = data.get("candidates") or []
    parts = ((candidates or [{}])[0].get("content") or {}).get("parts") or []
    raw = "".join(p.get("text", "") for p in parts).strip()
    if not raw:
        raise RuntimeError("gemini returned an empty response")

    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
    parsed = json.loads(raw)
    parsed_items = parsed.get("items") if isinstance(parsed, dict) else None
    if not isinstance(parsed_items, list):
        raise RuntimeError("gemini response JSON had no 'items' list")

    picks = []
    for entry in parsed_items[:5]:
        if not isinstance(entry, dict):
            continue
        url = str(entry.get("url", "")).strip()
        title = str(entry.get("title", "")).strip()
        summary = str(entry.get("summary", "")).strip()
        if not title or url not in whitelist:
            continue
        picks.append({"title": title, "summary": summary, "url": url})

    if not picks:
        raise RuntimeError("gemini returned no usable items")
    return picks


def _caption(number, row):
    """Rich-text photo caption: numbered heading-with-link + AI summary."""
    title = " ".join((row.get("title") or "").split())[:140] or "Read the article"
    parts = [
        f'{number}. <b><a href="{html.escape(row["url"], quote=True)}">'
        f'{html.escape(title)}</a></b>'
    ]
    summary = " ".join((row.get("summary") or "").split())[:400]
    if summary:
        parts.append(html.escape(summary))
    return "\n".join(parts)


def _rich_html(rows, label, note):
    """Build Rich-Message HTML: image block, heading-with-link, summary per item."""
    today = datetime.now(timezone.utc).strftime("%d %b %Y")
    parts = [
        f"<h2>📰 Daily digest — {html.escape(label)}</h2>",
        f"<footer>🗓 {today}</footer>",
    ]
    if note:
        parts.append(f"<p>{html.escape(note)}</p>")
    for number, row in enumerate(rows, 1):
        image = _clean_url(row.get("image"))
        if image:
            parts.append(f'<img src="{html.escape(image, quote=True)}"/>')
        title = " ".join((row.get("title") or "").split())[:140] or "Untitled"
        link = html.escape(row["url"], quote=True)
        parts.append(
            f"<h3>{number}. <a href=\"{link}\">{html.escape(title)}</a></h3>"
        )
        summary = " ".join((row.get("summary") or "").split())[:400]
        if summary:
            parts.append(f"<p>{html.escape(summary)}</p>")
    parts.append("<footer>✅ Digest complete — tap any headline to open the article.</footer>")
    return "\n".join(parts)


async def _send_rich(context, chat_id, thread, rows, label, note=None):
    """One sendRichMessage with all 5 stories, images, links and summaries.

    Uses the raw Bot API method (PTB 22.8 predates native rich messages).
    Raises on any API error — caller falls back to the album/text poster.
    """
    rich = {"html": _rich_html(rows, label, note)}
    await context.bot._post(
        "sendRichMessage",
        data={
            "chat_id": chat_id,
            "message_thread_id": thread,
            "rich_message": rich,
        },
    )


_ARTICLE_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
_ARTICLE_TIMEOUT = aiohttp.ClientTimeout(total=10, connect=5)
_MAX_HTML_BYTES = 400_000   # og:image lives in <head>; no need to read the page fully

_META_TAG_RE = re.compile(r"<meta\b[^>]*>", re.I)
_META_CONTENT_RE = re.compile(r"""content\s*=\s*["']([^"']+)["']""", re.I)
_JSONLD_IMG_RE = re.compile(
    r'"image"\s*:\s*"(https?://[^"\s]+\.(?:jpg|jpeg|png|webp|gif)[^"]*)"', re.I
)
_IMG_TAG_RE = re.compile(
    r"""<img[^>]+src\s*=\s*["']([^"']+)["']""", re.I
)


def _meta_tag_image(tag, base_url):
    low = tag.lower()
    is_og = ('property="og:image"' in low or "property='og:image'" in low)
    is_tw = ('name="twitter:image"' in low or "name='twitter:image'" in low or
             'name="twitter:image:src"' in low or "name='twitter:image:src'" in low)
    if not (is_og or is_tw):
        return None
    m = _META_CONTENT_RE.search(tag)
    if m and m.group(1).strip():
        return urljoin(base_url, m.group(1).strip())
    return None


def _extract_image_from_html(text, base_url):
    """og:image > twitter:image > JSON-LD image > first content <img>. Relative
    URLs are resolved, but relative-only <img> sources are skipped (Telegram
    media blocks need absolute http(s) URLs)."""
    for tag in _META_TAG_RE.findall(text):
        found = _meta_tag_image(tag, base_url)
        if found:
            return found
    m = _JSONLD_IMG_RE.search(text)
    if m:
        return m.group(1)
    m = _IMG_TAG_RE.search(text)
    if m:
        joined = urljoin(base_url, m.group(1))
        return joined if joined.startswith("http") else None
    return None


async def _article_image(url):
    """Fetch the article page and pull the og:image-style image (or None)."""
    try:
        async with aiohttp.ClientSession(
            timeout=_ARTICLE_TIMEOUT, headers={"User-Agent": _ARTICLE_UA}
        ) as session:
            async with session.get(url, allow_redirects=True) as resp:
                if resp.status != 200:
                    return None
                ctype = (resp.headers.get("Content-Type") or "").lower()
                data = b""
                async for chunk in resp.content.iter_chunked(65536):
                    data += chunk
                    if len(data) >= _MAX_HTML_BYTES:
                        break
                if "html" not in ctype and "xml" not in ctype and "text" not in ctype:
                    return None
                text = data.decode("utf-8", "ignore")
                return _extract_image_from_html(text, str(url))
    except Exception as exc:
        log.debug("article image fetch failed for %s: %s", url[:120], exc)
        return None


async def _enrich_images(rows, label):
    """Fill in missing images from the article pages themselves (og:image)."""
    missing = [r for r in rows if not _clean_url(r.get("image"))]
    if not missing:
        return
    got = 0
    for row in missing:
        img = await _article_image(row["url"])
        if img and _clean_url(img):
            row["image"] = img
            got += 1
    if got or len(missing) <= 5:
        log.info("digest images: +%d og:image from article pages (%s, %d were missing)",
                 got, label, len(missing))


def _text_block(numbered_rows, label, note):
    """Text digest for items that have no usable image (numbering preserved)."""
    today = datetime.now(timezone.utc).strftime("%d %b %Y")
    out = [f"📰 <b>Daily digest — {html.escape(label)}</b>", f"🗓 {today}"]
    if note:
        out.append(note)
    out.append("")
    for number, row in numbered_rows:
        title = html.escape(" ".join((row.get("title") or "Untitled").split())[:160])
        out.append(
            f'{number}. <b><a href="{html.escape(row["url"], quote=True)}">'
            f'{title}</a></b>'
        )
        summary = " ".join((row.get("summary") or "").split())[:400]
        if summary:
            out.append(html.escape(summary))
        out.append("")
    out.append("✅ Digest complete — tap a headline to read the full article.")
    return "\n".join(out).rstrip()


def _clean_url(url):
    """Telegram media URLs must be a direct http(s) link to an image."""
    url = str(url or "").strip()
    return url if url.startswith(("http://", "https://")) else ""


async def _post_digest(context, chat_id, thread, rows, label, note=None):
    """Post the digest: one photo album with rich-text captions, and a text
    block for any items without a usable image. Numbering runs across all 5."""
    bot = context.bot
    media_rows, text_rows = [], []
    for i, row in enumerate(rows, 1):
        image = _clean_url(row.get("image"))
        if image:
            media_rows.append((i, row, image))
        else:
            text_rows.append((i, row))

    async def send_single(n, row, image):
        await bot.send_photo(
            chat_id=chat_id,
            photo=image,
            caption=_caption(n, row)[:1024],
            parse_mode=ParseMode.HTML,
            message_thread_id=thread,
        )

    if media_rows:
        media = [
            InputMediaPhoto(
                media=image,
                caption=_caption(n, row)[:1024],
                parse_mode=ParseMode.HTML,
            )
            for n, row, image in media_rows
        ]
        try:
            if len(media) == 1:
                n, row, image = media_rows[0]
                await send_single(n, row, image)
                media_rows = []          # posted; not falling back to text
            else:
                await bot.send_media_group(
                    chat_id=chat_id,
                    media=media,
                    message_thread_id=thread,
                )
                media_rows = []          # posted; not falling back to text
        except Exception as exc:
            log.warning("album send failed (%s); trying items one by one", exc)
            dead = []
            for n, row, image in media_rows:
                try:
                    await send_single(n, row, image)
                except Exception as single_exc:
                    log.warning("photo send failed for %s (%s) — text fallback",
                                image[:120], single_exc)
                    dead.append((n, row))
            media_rows = []
            text_rows.extend(dead)

    if text_rows:
        await bot.send_message(
            chat_id=chat_id,
            message_thread_id=thread,
            text=_text_block(text_rows, label, note),
            parse_mode=ParseMode.HTML,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )


# ------------------------------------------------------------ commands ------

async def cmd_digestnow(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin: build & post the digest for THIS topic right now (for testing)."""
    msg = update.effective_message
    thread_id = get_thread_id(msg)
    items = db.get_pending(update.effective_chat.id, thread_id)
    if not items:
        await msg.reply_text(
            "📭 Nothing collected in this topic yet. Feeds collect quietly — "
            "lower /updatetime or wait a while, then /digestnow again.",
            parse_mode=ParseMode.HTML,
        )
        return

    class _Ctx:
        bot = update.get_bot()

    await process_topic(_Ctx(), update.effective_chat.id, thread_id)
    await msg.reply_text(
        f"✅ Digest attempted with {len(items)} collected article(s) — the result "
        "should be just above. If it says 'AI summary unavailable', check "
        "`journalctl -u bbbot | grep -i gemini` for the exact reason.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_timetopost(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    current = get_digest_time()

    if not ctx.args:
        await msg.reply_text(
            f"⏰ Daily digests are currently posted at <b>{current} UTC</b> in every "
            "topic with subscriptions.\n"
            "Feed checks are separate (/updatetime). "
            "Example to change: /timetopost 08:30",
            parse_mode=ParseMode.HTML,
        )
        return

    arg = ctx.args[0].strip()
    match = _TIME_RE.match(arg)
    if not match:
        await msg.reply_text(
            f"❌ <code>{html.escape(arg)}</code> is not a valid time.\n"
            "Use <b>HH:MM</b>, 24-hour clock, UTC — e.g. /timetopost 08:30 "
            f"(stays {current}).",
            parse_mode=ParseMode.HTML,
        )
        return

    normalized = f"{int(match.group(1)):02d}:{int(match.group(2)):02d}"
    if normalized == current:
        await msg.reply_text(
            f"ℹ️ Already set — digest posts daily at <b>{current} UTC</b>.",
            parse_mode=ParseMode.HTML,
        )
        return

    db.set_setting("digest_time_utc", normalized)
    reschedule_digest(ctx.job_queue, normalized)
    log.info("digest time changed %s -> %s UTC", current, normalized)
    await msg.reply_text(
        f"✅ Digest posting time updated: <b>{current} → {normalized} UTC</b>.\n"
        f"The next digest for every topic with collected articles goes out at "
        f"<b>{normalized} UTC</b>. Saved — survives restarts.",
        parse_mode=ParseMode.HTML,
    )


def build_handler():
    return CommandHandler("timetopost", cmd_timetopost)


# Both admin commands are wrapped here, after their definitions.
cmd_digestnow = admin_command()(cmd_digestnow)
cmd_timetopost = admin_command()(cmd_timetopost)
