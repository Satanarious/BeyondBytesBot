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

import aiohttp
from telegram import LinkPreviewOptions, Update
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

    log.info("digest: %d collected for '%s' (model %s)", len(items), label,
             os.getenv("GEMINI_MODEL") or "gemini-3.1-pro-preview")
    try:
        picks = await summarize_with_gemini(items)
        text = _render_digest(picks, label)
    except Exception as exc:
        log.warning("gemini digest failed for topic %s: %s", label, exc)
        text = _fallback_digest(items, label)

    await context.bot.send_message(
        chat_id=chat_id,
        message_thread_id=send_thread,
        text=text,
        parse_mode=ParseMode.HTML,
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )
    db.clear_pending(chat_id, thread_id)
    log.info("digest posted for chat %s topic %s (%d collected)",
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

    model = (os.getenv("GEMINI_MODEL") or "gemini-3.1-pro-preview").strip()
    whitelist = {row["link"] for row in items}
    lines = "\n".join(f"- {row['title']} — {row['link']}" for row in items)
    prompt = PROMPT_TEMPLATE.format(lines=lines)

    gen_config = {
        "temperature": 0.3,
        "maxOutputTokens": 4096,
        "responseMimeType": "application/json",
    }
    if "pro" not in model:
        # Flash models support disabling thinking; 2.5 Pro REJECTS budget 0
        # (thinking is mandatory there) and would 400 on every request.
        gen_config["thinkingConfig"] = {"thinkingBudget": 0}

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": gen_config,
    }

    data = None
    status = None
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
        for attempt in (1, 2, 3):
            async with session.post(
                _GEMINI_URL.format(model=model),
                json=payload,
                headers={"x-goog-api-key": api_key},
            ) as resp:
                status = resp.status
                if status == 200:
                    data = await resp.json()
                    break
                if status in _RETRYABLE_STATUS and attempt < 3:
                    wait = 30 * attempt
                    log.warning("gemini HTTP %s (attempt %d/3, topic pending); "
                                "retrying in %ss", status, attempt, wait)
                elif status in _RETRYABLE_STATUS:
                    body = (await resp.text())[:200]
                    raise RuntimeError(
                        f"gemini HTTP {status} after {attempt} attempts: {body}"
                    )
                else:
                    body = (await resp.text())[:300]
                    raise RuntimeError(f"gemini HTTP {status}: {body}")
            await asyncio.sleep(wait)
        else:
            raise RuntimeError(f"gemini kept failing (last HTTP {status})")

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


def _render_digest(picks, label):
    today = datetime.now(timezone.utc).strftime("%d %b %Y")
    out = [f"📰 <b>Daily digest — {html.escape(label)}</b>", f"🗓 {today}", ""]
    for i, item in enumerate(picks, 1):
        title = html.escape(" ".join(item["title"].split())[:160])
        summary = " ".join(item.get("summary", "").split())[:400]
        out.append(f"{i}. <b><a href=\"{html.escape(item['url'])}\">{title}</a></b>")
        if summary:
            out.append(html.escape(summary))
        out.append("")
    out.append("✅ Digest complete — tap a headline to read the full article.")
    return "\n".join(out).rstrip()


def _fallback_digest(items, label):
    """If Gemini is unreachable, post today's links plainly instead of losing them."""
    today = datetime.now(timezone.utc).strftime("%d %b %Y")
    out = [
        f"📰 <b>Daily digest — {html.escape(label)}</b>",
        f"🗓 {today}",
        "⚠️ AI summary unavailable today — here are the collected articles:",
        "",
    ]
    for i, row in enumerate(items[:5], 1):
        title = html.escape((row["title"] or "Untitled")[:160])
        out.append(f"{i}. <b><a href=\"{html.escape(row['link'])}\">{title}</a></b>")
    return "\n".join(out)


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
