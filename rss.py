"""RSS feed commands and background poller for BBbot."""

import asyncio
import html
import logging
import re

import aiohttp
import feedparser
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

import db
from digest import get_digest_time
from guard import admin_command, allowed_chat_id, is_meant_for
from utils import get_thread_id, humanize_duration

log = logging.getLogger("bbbot.rss")

POLL_JOB_NAME = "rss_poller"
POLL_MIN = 30            # seconds
POLL_MAX = 24 * 3600     # seconds

_UA = "BBbot/1.0 (RSS reader bot for a Telegram group)"
_TIMEOUT = aiohttp.ClientTimeout(total=30)
_MAX_LINKS_PER_POLL = 5   # max new articles sent per feed per poll
_MAX_NEW_SCAN = 50        # how many newest entries we scan per fetch

_URL_RE = re.compile(r"^https?://", re.I)


async def _fetch(url):
    async with aiohttp.ClientSession(
        timeout=_TIMEOUT, headers={"User-Agent": _UA}
    ) as session:
        async with session.get(url) as resp:
            resp.raise_for_status()
            return await resp.read()


async def fetch_feed(url):
    """Download + parse a feed. Returns (feed_title, [(link, title), ...] newest first)."""
    raw = await _fetch(url)
    parsed = await asyncio.to_thread(feedparser.parse, raw)
    entries = parsed.entries or []
    if not entries:
        detail = ""
        if getattr(parsed, "bozo", False):
            detail = f" ({str(parsed.bozo_exception)[:200]})"
        raise ValueError(f"no entries found in feed{detail}")

    feed_title = None
    try:
        feed_title = (parsed.feed.get("title") or "").strip()
    except Exception:
        pass

    links = []
    for entry in entries[:_MAX_NEW_SCAN]:
        link = entry.get("link") or entry.get("id")
        link = str(link).strip() if link else None
        if not link or not link.startswith("http"):
            continue
        title = (entry.get("title") or "").strip() or "Untitled"
        links.append((link, title))
    if not links:
        raise ValueError("feed has no entries with usable links")
    return (feed_title or url), links


# ------------------------------------------------------------- poller -----

async def check_feed(context, sub):
    """Fetch one subscription; queue unseen entries for this topic's daily digest."""
    if not is_meant_for(sub["chat_id"]):
        return  # other chats don't get polled (or digested) at all
    feed_title, entries = await fetch_feed(sub["url"])
    if not sub["title"] and feed_title:
        db.update_subscription_title(sub["id"], feed_title)

    seen = db.seen_links(sub["id"])
    for link, title in entries:
        if link in seen:
            continue
        db.add_pending(sub["chat_id"], sub["thread_id"], link, title)
        db.add_seen_link(sub["id"], link)  # never offered twice, even across digests


async def poll_all(context: ContextTypes.DEFAULT_TYPE):
    subs = db.all_subscriptions()
    if not subs:
        return
    for sub in subs:
        try:
            await check_feed(context, sub)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning(
                "feed check failed for %s (sub %s, topic %s): %s",
                sub["url"], sub["id"], sub["thread_id"], exc,
            )


# --------------------------------------------------------------- schedule ---

def get_poll_interval(default=300):
    """Persisted poll interval in seconds (clamped to sane bounds)."""
    try:
        interval = int(db.get_setting("rss_poll_seconds", str(default)))
    except (TypeError, ValueError):
        interval = int(default)
    return max(min(interval, POLL_MAX), POLL_MIN)


def reschedule_poll(job_queue, seconds):
    """Swap the running repeating poller job for one with the new interval."""
    for job in job_queue.jobs():
        if job.name == POLL_JOB_NAME:
            job.schedule_removal()
    job_queue.run_repeating(poll_all, interval=seconds, first=5, name=POLL_JOB_NAME)


_INTERVAL_TOKEN = re.compile(r"(\d+)\s*([smh]?)", re.I)


async def cmd_updatetime(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    current = get_poll_interval()

    if not ctx.args:
        await msg.reply_text(
            f"⏱ RSS feeds are currently checked every "
            f"<b>{humanize_duration(current)}</b> ({current}s).\n"
            "To change it: /updatetime 90 (seconds) or /updatetime 2m, "
            "/updatetime 10m, /updatetime 1h\n"
            f"Allowed: {POLL_MIN}s – 24h. The change is saved and applies immediately.",
            parse_mode=ParseMode.HTML,
        )
        return

    tokens = _INTERVAL_TOKEN.findall(" ".join(ctx.args))
    if not tokens:
        await msg.reply_text(
            "Usage: /updatetime 90  (seconds) — or with units: /updatetime 5m, /updatetime 1h"
        )
        return
    seconds = sum(
        int(num) * {"": 1, "s": 1, "m": 60, "h": 3600}[unit.lower()]
        for num, unit in tokens
    )
    if seconds < POLL_MIN or seconds > POLL_MAX:
        await msg.reply_text(
            f"❌ Out of range. Pick anything from <code>{POLL_MIN}</code> seconds "
            f"up to 24 hours.",
            parse_mode=ParseMode.HTML,
        )
        return

    db.set_setting("rss_poll_seconds", seconds)
    reschedule_poll(update.get_bot().job_queue, seconds)
    log.info("RSS poll interval changed to %ss", seconds)
    await msg.reply_text(
        f"✅ RSS update check set to every <b>{humanize_duration(seconds)}</b> "
        f"({seconds}s). Previously: {current}s. Saved — survives restarts.",
        parse_mode=ParseMode.HTML,
    )


# ------------------------------------------------------------ commands -----

def _topic_label(titles, thread_id):
    if thread_id == 1:
        return titles.get(1) or "General"
    return titles.get(thread_id) or f"Topic #{thread_id}"


async def cmd_subscribe(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    thread_id = get_thread_id(msg)

    if chat.type not in ("group", "supergroup"):
        await msg.reply_text(
            "Use /subscribe inside a topic of the Beyond Bytes group to subscribe "
            "that topic to a feed."
        )
        return

    url = " ".join(ctx.args).strip().strip("<>") if ctx.args else ""
    if not _URL_RE.match(url):
        await msg.reply_text(
            "Usage: /subscribe &lt;rss_url&gt;\n"
            "Example: /subscribe https://animenews.example/feed.xml",
            parse_mode=ParseMode.HTML,
        )
        return

    if any(s["url"] == url for s in db.subscriptions_for_topic(chat.id, thread_id)):
        await msg.reply_text("⚠️ This topic is already subscribed to that feed. See /list")
        return

    status = await msg.reply_text("🔎 Checking the feed…")
    try:
        feed_title, entries = await fetch_feed(url)
    except Exception as exc:
        log.warning("subscribe failed for %s: %s", url, exc)
        await status.edit_text(
            f"❌ Could not read an RSS feed at that URL.\n"
            f"<code>{html.escape(str(exc)[:300])}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    sub_id = db.add_subscription(chat.id, thread_id, url, feed_title)
    # Treat the current backlog as already seen so we only announce new articles.
    for link, _ in entries:
        db.add_seen_link(sub_id, link)

    await status.edit_text(
        f"✅ Subscribed! <b>{html.escape(feed_title)}</b>\n"
        f"New articles are collected quietly for this topic, and a daily AI "
        f"digest (top stories, summarized) is posted here once a day.\n"
        f"Digest posting time: <b>{get_digest_time()}</b> UTC — /timetopost to "
        f"change it.\nSubscription ID: <code>{sub_id}</code>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_unsubscribe(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    arg = ctx.args[0].strip().strip("<>") if ctx.args else ""
    subs = db.subscriptions_for_chat(chat.id)

    target = None
    if arg.isdigit():
        target = next((s for s in subs if str(s["id"]) == arg), None)
    elif arg:
        target = next((s for s in subs if s["url"] == arg), None)

    if target is None:
        await msg.reply_text(
            "Not found. Pass an ID or URL from /list, e.g. /unsubscribe 3"
        )
        return

    db.delete_subscription(chat.id, target["id"])
    await msg.reply_text(
        f"✅ Unsubscribed <b>{html.escape(target['title'] or target['url'])}</b>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    subs = db.subscriptions_for_topic(chat.id, get_thread_id(msg))
    if not subs:
        await msg.reply_text(
            "📭 No RSS sources in this topic yet.\n"
            "Use /subscribe <rss_url> to add one."
        )
        return
    lines = [f"📡 RSS sources in this topic ({len(subs)}):", ""]
    for s in subs:
        lines.append(f"{s['id']}. {html.escape(s['title'] or s['url'])}")
        lines.append(f"    <code>{html.escape(s['url'])}</code>")
    if allowed_chat_id() == 0:
        lines.append("")
        lines.append(
            f"ℹ️ <i>Not locked to a group yet — set <code>BB_GROUP_ID={chat.id}</code> "
            "in /opt/bbbot/.env to make BBbot Beyond-Bytes-only.</i>"
        )
    await msg.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_listall(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    subs = db.subscriptions_for_chat(chat.id)
    if not subs:
        await msg.reply_text("📭 No RSS sources subscribed anywhere in this group.")
        return
    titles = {t["thread_id"]: t["title"] for t in db.topics_for_chat(chat.id)}
    current_thread = get_thread_id(msg)

    grouped = {}
    for s in subs:
        grouped.setdefault(s["thread_id"], []).append(s)

    lines = [f"📚 RSS sources in all topics ({len(subs)} total):", ""]
    for thread_id in sorted(grouped):
        marker = " ▸" if thread_id == current_thread else ""
        lines.append(f"<b>{_topic_label(titles, thread_id)}</b>{marker}")
        for s in grouped[thread_id]:
            lines.append(f"  {s['id']}. {html.escape(s['title'] or s['url'])}")
        lines.append("")
    await msg.reply_text("\n".join(lines).rstrip(), parse_mode=ParseMode.HTML)

# Command handlers individually; applied from the bottom so feature logic
# stays plain (also keeps the ConversationHandler-free module tidy).
for _name in ("cmd_subscribe", "cmd_unsubscribe", "cmd_list", "cmd_listall",
              "cmd_updatetime"):
    globals()[_name] = admin_command()(globals()[_name])
