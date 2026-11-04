"""Giveaway/gift feature for BBbot: /createnewgiveaway wizard and the draw.

Note: the database tables still carry the old "raffles"/"participants" names for
data compatibility; everything user-facing says "giveaway".
"""

import html
import logging
import random
import re
from datetime import datetime, timedelta, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import db
from utils import get_thread_id, humanize_duration

log = logging.getLogger("bbbot.giveaway")

NAME, DESC, IMAGE, DURATION = range(4)

_DUR_TOKEN = re.compile(r"(\d+)\s*(d|h|m)", re.I)
MIN_DURATION = 5 * 60          # 5 minutes
MAX_DURATION = 90 * 86400      # 90 days
JOB_NAME_FMT = "raffle:{}"     # scheduled-job name format (internal, unchanged)


def _parse_duration(text):
    """'1d 12h' -> seconds, or None if invalid."""
    tokens = _DUR_TOKEN.findall(text or "")
    if not tokens:
        return None
    total = 0
    for num, unit in tokens:
        total += int(num) * {"d": 86400, "h": 3600, "m": 60}[unit.lower()]
    if total < MIN_DURATION or total > MAX_DURATION:
        return None
    return total


# ----------------------------------------------------------- wizard -------
#
# The wizard is bound to the topic where /createnewgiveaway was run. Every
# prompt + user answer is tracked in draft["cleanup"] and deleted after the
# giveaway announcement is posted (or on /cancel).

async def _precheck(update: Update, ctx: ContextTypes.DEFAULT_TYPE, state):
    """Wizard steps must come from the topic where /createnewgiveaway was run.

    Returns None to proceed, or a state to stay in (wrong topic), or
    ConversationHandler.END if there is no draft at all.
    """
    draft = ctx.user_data.get("giveaway_draft")
    if draft is None:
        await update.effective_message.reply_text(
            "⚠️ No giveaway creation in progress. Start one with /createnewgiveaway."
        )
        return ConversationHandler.END
    if get_thread_id(update.effective_message) != draft["thread_id"]:
        await update.effective_message.reply_text(
            "⛔ This giveaway is being created in a different topic. Continue it "
            "there, or run /cancel to abort it.",
            parse_mode=ParseMode.HTML,
        )
        return state
    return None


def _track_user_msg(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Remember a user message so it can be deleted together with the rest of
    the wizard chat once the giveaway is created."""
    draft = ctx.user_data.get("giveaway_draft")
    if draft is not None:
        draft.setdefault("cleanup", []).append(update.effective_message.message_id)


async def _send_prompt(update: Update, ctx: ContextTypes.DEFAULT_TYPE, text,
                       reply_markup=None):
    """Post a wizard prompt into the draft's topic and track it for deletion."""
    draft = ctx.user_data["giveaway_draft"]
    thread = draft["thread_id"] if draft["thread_id"] != 1 else None
    sent = await update.get_bot().send_message(
        chat_id=update.effective_chat.id,
        text=text,
        parse_mode=ParseMode.HTML,
        message_thread_id=thread,
        reply_markup=reply_markup,
    )
    draft.setdefault("cleanup", []).append(sent.message_id)
    return sent


async def _cleanup_wizard(bot, chat, draft):
    for message_id in draft.pop("cleanup", []):
        try:
            await bot.delete_message(chat_id=chat.id, message_id=message_id)
        except Exception as exc:
            log.debug("could not delete wizard message %s: %s", message_id, exc)


async def cmd_new(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["giveaway_draft"] = {
        "thread_id": get_thread_id(update.effective_message),
        "image_file_id": None,
    }
    _track_user_msg(update, ctx)
    await _send_prompt(
        update, ctx,
        "🎁 Starting a new giveaway for this topic!\n\n"
        "<b>Step 1/4 — Name:</b> what is the prize called?",
    )
    return NAME


async def got_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    block = await _precheck(update, ctx, NAME)
    if block is not None:
        return block
    _track_user_msg(update, ctx)
    ctx.user_data["giveaway_draft"]["name"] = update.effective_message.text.strip()[:120]
    await _send_prompt(
        update, ctx,
        "<b>Step 2/4 — Description:</b> describe the giveaway briefly (or /skip).",
    )
    return DESC


async def got_desc(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    block = await _precheck(update, ctx, DESC)
    if block is not None:
        return block
    _track_user_msg(update, ctx)
    ctx.user_data["giveaway_draft"]["description"] = (
        update.effective_message.text.strip()[:800]
    )
    await _send_prompt(
        update, ctx,
        "<b>Step 3/4 — Image:</b> send a picture of the prize (or /skip).",
    )
    return IMAGE


async def skip_desc(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    block = await _precheck(update, ctx, DESC)
    if block is not None:
        return block
    _track_user_msg(update, ctx)
    ctx.user_data["giveaway_draft"]["description"] = None
    await _send_prompt(
        update, ctx,
        "<b>Step 3/4 — Image:</b> send a picture of the prize (or /skip).",
    )
    return IMAGE


async def got_image(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    block = await _precheck(update, ctx, IMAGE)
    if block is not None:
        return block
    _track_user_msg(update, ctx)
    ctx.user_data["giveaway_draft"]["image_file_id"] = (
        update.effective_message.photo[-1].file_id
    )
    return await _ask_duration(update, ctx)


async def skip_image(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    block = await _precheck(update, ctx, IMAGE)
    if block is not None:
        return block
    _track_user_msg(update, ctx)
    return await _ask_duration(update, ctx)


async def _ask_duration(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    prefix = "🖼 Image saved.\n\n" if update.effective_message.photo else ""
    await _send_prompt(
        update, ctx,
        prefix
        + "<b>Step 4/4 — Duration:</b> how long should the giveaway run?\n"
        "Send something like <code>45m</code>, <code>2h</code>, "
        "<code>1d 12h</code> (5 minutes – 90 days).",
    )
    return DURATION


async def got_duration(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    block = await _precheck(update, ctx, DURATION)
    if block is not None:
        return block
    _track_user_msg(update, ctx)
    seconds = _parse_duration(update.effective_message.text)
    if seconds is None:
        await _send_prompt(
            update, ctx,
            "🤔 I couldn't read that as a duration.\n"
            "Try again, e.g. <code>30m</code>, <code>2h</code> or <code>3d</code> "
            "between 5 minutes and 90 days.",
        )
        return DURATION

    ctx.user_data["giveaway_draft"]["duration_s"] = seconds

    await _create_giveaway(
        update.get_bot(), ctx.job_queue, update.effective_chat,
        ctx.user_data.pop("giveaway_draft"),
    )
    return ConversationHandler.END


async def _create_giveaway(bot, job_queue, chat, draft):
    thread_id = draft["thread_id"]
    send_thread = thread_id if thread_id != 1 else None
    now = datetime.now(timezone.utc)
    end = now + timedelta(seconds=draft["duration_s"])

    giveaway_id = db.create_raffle(
        chat.id, thread_id, draft["name"], draft.get("description"),
        draft.get("image_file_id"), "join", end.isoformat(),
    )

    text = (
        f"🎁 <b>{html.escape(draft['name'])}</b>\n\n"
        + (f"{html.escape(draft['description'])}\n\n" if draft.get("description") else "")
        + f"⏳ Ends in <b>{humanize_duration(draft['duration_s'])}</b> "
        f"({end:%Y-%m-%d %H:%M} UTC)\n"
        "🔘 Only members who tap <b>🎟 Join</b> below will be enrolled."
    )

    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton(
            "🎟 Join Giveaway", callback_data=f"rjoin:{giveaway_id}"
        )]]
    )

    if draft.get("image_file_id"):
        sent = await bot.send_photo(
            chat_id=chat.id, photo=draft["image_file_id"], caption=text,
            parse_mode=ParseMode.HTML, message_thread_id=send_thread,
            reply_markup=keyboard,
        )
    else:
        sent = await bot.send_message(
            chat_id=chat.id, text=text, parse_mode=ParseMode.HTML,
            message_thread_id=send_thread, reply_markup=keyboard,
        )
    db.set_announcement_message(giveaway_id, sent.message_id)

    delay = max((end - datetime.now(timezone.utc)).total_seconds(), 2.0)
    job_queue.run_once(
        draw_job, when=delay, data={"raffle_id": giveaway_id},
        name=JOB_NAME_FMT.format(giveaway_id),
    )
    log.info("giveaway %s created in chat %s topic %s (%s)",
             giveaway_id, chat.id, thread_id, mode)

    # The giveaway is live — wipe the whole creation wizard chat.
    await _cleanup_wizard(bot, chat, draft)


# ------------------------------------------------------------ helpers -----

def _thread_for(giveaway):
    return giveaway["thread_id"] if giveaway["thread_id"] != 1 else None


async def cancel_giveaway_job(job_queue, giveaway_id, status):
    """Remove the scheduled draw job (if present) and close the giveaway."""
    for job in job_queue.jobs():
        if job.name == JOB_NAME_FMT.format(giveaway_id):
            job.schedule_removal()
    db.finish_raffle(giveaway_id, status)


# ------------------------------------------------------------- events -----

async def on_join_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    giveaway_id = int(ctx.match.group(1))
    giveaway = db.get_raffle(giveaway_id)

    if not giveaway or giveaway["status"] != "running":
        await query.answer("This giveaway has already ended.")
        return

    display = f"@{user.username}" if user.username else (user.full_name or str(user.id))
    added = db.add_participant(giveaway_id, user.id, display)
    await query.answer("🎟 You're in — good luck!" if added else "You are already enrolled 🎟")


async def draw_job(context: ContextTypes.DEFAULT_TYPE):
    giveaway_id = context.job.data["raffle_id"]
    giveaway = db.get_raffle(giveaway_id)
    if not giveaway or giveaway["status"] != "running":
        return

    parts = db.participants(giveaway_id)
    name = html.escape(giveaway["name"])
    thread = _thread_for(giveaway)

    if not parts:
        db.finish_raffle(giveaway_id, "finished")
        await context.bot.send_message(
            chat_id=giveaway["chat_id"], message_thread_id=thread,
            text=f"🎲 <b>{name}</b> — time's up! Nobody was enrolled, so no winner this time.",
            parse_mode=ParseMode.HTML,
        )
        return

    winner = random.choice(parts)
    db.finish_raffle(giveaway_id, "finished", winner["user_id"], winner["display_name"])
    mention = (
        f'<a href="tg://user?id={winner["user_id"]}">'
        f'{html.escape(winner["display_name"])}</a>'
    )
    plural = "" if len(parts) == 1 else "s"
    await context.bot.send_message(
        chat_id=giveaway["chat_id"], message_thread_id=thread,
        text=(
            "🎉 <b>WE HAVE A WINNER!</b>\n\n"
            f"Out of {len(parts)} participant{plural}, the winner of "
            f"<b>{name}</b> is…\n\n"
            f"🏆 {mention}\n\n"
            "BIG congratulations! 🥳🎊 You've won "
            f"<b>{name}</b> — redeem your prize!"
        ),
        parse_mode=ParseMode.HTML,
    )
    log.info("giveaway %s finished, winner %s", giveaway_id, winner["user_id"])


async def on_group_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Silently tracks topic names so /listall can show them (with or without
    the member ever running a command before)."""
    msg = update.effective_message
    chat = update.effective_chat
    if msg is None or chat.type not in ("group", "supergroup"):
        return

    thread_id = get_thread_id(msg)
    if msg.forum_topic_created is not None:
        db.upsert_topic(chat.id, thread_id, msg.forum_topic_created.name)


# ------------------------------------------------------------ commands -----

async def cmd_listgiveaways(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    giveaways = db.running_raffles(chat_id=chat.id)
    if not giveaways:
        await update.effective_message.reply_text(
            "🎁 No running giveaways. Start one with /createnewgiveaway!"
        )
        return
    titles = {t["thread_id"]: t["title"] for t in db.topics_for_chat(chat.id)}
    lines = ["🎁 Running giveaways:", ""]
    for g in giveaways:
        end_dt = datetime.fromisoformat(g["ends_at"])
        remaining = humanize_duration(
            max((end_dt - datetime.now(timezone.utc)).total_seconds(), 0)
        )
        label = titles.get(
            g["thread_id"],
            f"Topic #{g['thread_id']}" if g["thread_id"] != 1 else "General",
        )
        lines.append(
            f"<b>{html.escape(g['name'])}</b> (ID <code>{g['id']}</code>) — {label}\n"
            f"  ends in {remaining}, {len(db.participants(g['id']))} enrolled"
        )
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_cancelgiveaway(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    arg = ctx.args[0] if ctx.args else ""
    if not arg.isdigit():
        await update.effective_message.reply_text(
            "Usage: /cancelgiveaway &lt;id&gt; — get the ID from /listgiveaways",
            parse_mode=ParseMode.HTML,
        )
        return
    giveaway = db.get_raffle(int(arg))
    if (not giveaway or giveaway["chat_id"] != update.effective_chat.id
            or giveaway["status"] != "running"):
        await update.effective_message.reply_text("No running giveaway with that ID.")
        return
    await cancel_giveaway_job(ctx.job_queue, giveaway["id"], "cancelled")
    await update.effective_message.reply_text(
        f"✅ Giveaway <code>{giveaway['id']}</code> — "
        f"<b>{html.escape(giveaway['name'])}</b> cancelled.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_cancel_conversation(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    draft = ctx.user_data.pop("giveaway_draft", None)
    if draft is not None:
        await _cleanup_wizard(update.get_bot(), update.effective_chat, draft)
        await update.effective_message.reply_text("❌ Giveaway creation cancelled.")
    else:
        await update.effective_message.reply_text(
            "❌ Giveaway creation cancelled."
        )
    return ConversationHandler.END


def build_conversation():
    return ConversationHandler(
        entry_points=[CommandHandler("createnewgiveaway", cmd_new)],
        states={
            NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_name)],
            DESC: [
                CommandHandler("skip", skip_desc),
                MessageHandler(filters.TEXT & ~filters.COMMAND, got_desc),
            ],
            IMAGE: [
                CommandHandler("skip", skip_image),
                MessageHandler(filters.PHOTO, got_image),
            ],
            DURATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_duration)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel_conversation)],
        allow_reentry=True,
    )
