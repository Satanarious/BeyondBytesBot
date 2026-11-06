"""Access control for BBbot.

Two rules, applied to every command handler via the admin_command decorator:
1. The bot only answers in the Beyond Bytes group (anything else gets
   "Not meant for this group").
2. Only group admins can run commands.

The target group is resolved by BB_GROUP_ID in .env (explicit), or
auto-locked to the one chat BBbot has data for. When nothing is locked yet
(first run), commands anywhere show the chat ID so it can be locked next.
"""

import html
import logging
import os

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

import db

log = logging.getLogger("bbbot.guard")

DENIAL = "🔒 You should be an admin to use this command."
WRONG_GROUP = "❌ Not meant for this group — BBbot works only in the Beyond Bytes group."


def allowed_chat_id():
    """0 = not locked (first run); otherwise the only allowed supergroup id."""
    env = (os.getenv("BB_GROUP_ID") or "").strip()
    if env:
        try:
            return int(env)
        except ValueError:
            log.warning("BB_GROUP_ID=%r is not an integer; ignoring", env)
    try:
        return int(db.get_setting("locked_chat_id") or 0)
    except ValueError:
        return 0


def is_meant_for(chat_id):
    allowed = allowed_chat_id()
    return allowed == 0 or chat_id == allowed


def auto_lock_if_possible():
    """On startup: if nothing is locked yet and all stored data points at one
    chat, lock to it automatically. Returns the locked id or 0."""
    if (os.getenv("BB_GROUP_ID") or "").strip():
        return allowed_chat_id()
    if db.get_setting("locked_chat_id"):
        return allowed_chat_id()

    chat_ids = {s["chat_id"] for s in db.all_subscriptions()}
    raffle_chats = {r["chat_id"] for r in db.running_raffles()}
    chat_ids |= {r["chat_id"] for r in db.all_raffle_chats()}
    chat_ids |= raffle_chats
    if len(chat_ids) == 1:
        chat_id = chat_ids.pop()
        db.set_setting("locked_chat_id", chat_id)
        log.info("auto-locked BBbot to chat %s (only chat with stored data; "
                 "set BB_GROUP_ID in .env to override)", chat_id)
        return chat_id
    return 0


def _discovery_footer(chat):
    if allowed_chat_id() == 0:
        return ("\n\nℹ️ This bot is not locked to a group yet. To make it "
                f"Beyond-Bytes-only, set <code>BB_GROUP_ID={chat.id}</code> in "
                "/opt/bbbot/.env and restart (or wait — it auto-locks once it "
                "has data for this group).")
    return ""


async def _checks_pass(update: Update, context: ContextTypes.DEFAULT_TYPE, chat):
    user = update.effective_user

    if chat.type == "private":
        try:
            await update.effective_message.reply_text(
                "❌ Not meant for this chat — BBbot works only inside the "
                "Beyond Bytes group.", parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
        return False

    if not is_meant_for(chat.id):
        try:
            await update.effective_message.reply_text(
                WRONG_GROUP, parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
        return False

    # Admin check
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
    except Exception as exc:
        log.warning("admin check failed in %s for %s: %s", chat.id, user.id, exc)
        try:
            await update.effective_message.reply_text(DENIAL)
        except Exception:
            pass
        return False

    if member.status not in ("administrator", "creator"):
        try:
            await update.effective_message.reply_text(
                DENIAL + _discovery_footer(chat), parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
        return False
    return True


def admin_command(block_result=None):
    """Decorator: restrict a command handler to the locked group + admins.

    block_result: what the wrapper returns when access is denied (e.g.
    ConversationHandler.END for conversation entry points).
    """
    def deco(fn):
        async def wrapper(update, context):
            chat = update.effective_chat
            if chat is None:
                return block_result
            if chat.type == "channel":
                return None  # silently ignore channel posts from other channels
            if not await _checks_pass(update, context, chat):
                return block_result
            return await fn(update, context)
        wrapper.__name__ = getattr(fn, "__name__", "admin_command")
        return wrapper
    return deco
