"""BBbot — BeyondBytes bot.

Single-group Telegram bot with forum topics:
  1. Per-topic RSS subscriptions (/subscribe, /unsubscribe, /list, /listall)
  2. Giveaways/gifts (/createnewgiveaway, /listgiveaways, /cancelgiveaway)
"""

import logging
import os
from datetime import datetime, timezone

from dotenv import load_dotenv
from telegram import BotCommand, Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    Defaults,
    MessageHandler,
    filters,
)

import db
import digest
import giveaway
import guard
import media
import rss

log = logging.getLogger("bbbot")


async def post_init(app):
    db.init_db()

    # RSS poller: honor the interval saved via /updatetime; seed it from .env
    # (RSS_POLL_SECONDS) on the very first start.
    try:
        default_interval = max(int(os.getenv("RSS_POLL_SECONDS", "300")), 60)
    except ValueError:
        default_interval = 300
    if db.get_setting("rss_poll_seconds") is None:
        db.set_setting("rss_poll_seconds", default_interval)
    interval = rss.get_poll_interval(default_interval)
    rss.reschedule_poll(app.job_queue, interval)
    log.info("RSS poll interval: %ss", interval)

    locked = guard.auto_lock_if_possible()
    log.info("group lock: %s",
             f"locked to {locked}" if locked else "NOT LOCKED (first run — will auto-lock once data exists; BB_GROUP_ID env to force)")

    digest_hhmm = digest.reschedule_digest(app.job_queue)
    log.info("Daily digest time: %s UTC | model: %s | gap between topics: %ss | "
             "Gemini key %s",
             digest_hhmm,
             os.getenv("GEMINI_MODEL") or "gemini-3.5-flash-lite",
             digest.DIGEST_GAP,
             "loaded" if os.getenv("GEMINI_API_KEY") else "MISSING — digests will fall back to plain links")

    await app.bot.set_my_commands(
        [
            BotCommand("subscribe", "Subscribe this topic to an RSS feed"),
            BotCommand("unsubscribe", "/unsubscribe <id|url>"),
            BotCommand("list", "List RSS sources in this topic"),
            BotCommand("listall", "List RSS sources in all topics"),
            BotCommand("updatetime", "Change RSS check interval: /updatetime 5m"),
            BotCommand("timetopost", "Set daily digest time (UTC): /timetopost 08:30"),
            BotCommand("digestnow", "Build this topic's digest now (admin, for testing)"),
            BotCommand("createnewgiveaway", "Start a giveaway in this topic"),
            BotCommand("listgiveaways", "Show running giveaways"),
            BotCommand("cancelgiveaway", "/cancelgiveaway <id>"),
            BotCommand("cancel", "Cancel the current operation"),
            BotCommand("anime", "Anime info (MAL→AniList): /anime chainsaw man"),
            BotCommand("eac", "Manga/manhwa/manhua: /eac solo leveling"),
            BotCommand("books", "Book info (OpenLibrary): /books dune"),
            BotCommand("comics", "Comics (ComicVine→OpenLibrary): /comics batman"),
            BotCommand("imdb", "Movies & TV (IMDb): /imdb inception"),
        ]
    )

    # Re-schedule draws for giveaways that were running before a restart.
    now = datetime.now(timezone.utc)
    restored = 0
    for r in db.running_raffles():
        try:
            end = datetime.fromisoformat(r["ends_at"])
            delay = max((end - now).total_seconds(), 2)
            app.job_queue.run_once(
                giveaway.draw_job,
                when=delay,
                data={"raffle_id": r["id"]},
                name=giveaway.JOB_NAME_FMT.format(r["id"]),
            )
            restored += 1
        except Exception:
            log.exception("could not restore giveaway %s", r["id"])
    log.info("restored %d running giveaways", restored)
    log.info("BBbot is up and running")


def main():
    load_dotenv()

    token = os.getenv("BOT_TOKEN")
    if not token or "your" in token.lower():
        raise SystemExit(
            "BOT_TOKEN is missing. Copy .env.example to .env and paste the token "
            "you get from @BotFather."
        )

    app = (
        ApplicationBuilder()
        .token(token)
        .defaults(Defaults(do_quote=False))
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("updatetime", rss.cmd_updatetime))
    app.add_handler(digest.build_handler())
    app.add_handler(CommandHandler("digestnow", digest.cmd_digestnow))
    app.add_handler(CommandHandler("subscribe", rss.cmd_subscribe))
    app.add_handler(CommandHandler("unsubscribe", rss.cmd_unsubscribe))
    app.add_handler(CommandHandler("list", rss.cmd_list))
    app.add_handler(CommandHandler("listall", rss.cmd_listall))
    app.add_handler(CommandHandler("listgiveaways", giveaway.cmd_listgiveaways))
    app.add_handler(CommandHandler("cancelgiveaway", giveaway.cmd_cancelgiveaway))

    app.add_handler(giveaway.build_conversation())                     # group 0
    for handler in media.build_handlers():                             # group 0
        app.add_handler(handler)
    app.add_handler(                                                   # group 1
        CallbackQueryHandler(giveaway.on_join_button, pattern=r"^rjoin:(\d+)$")
    )
    app.add_handler(                                                   # group 1
        CallbackQueryHandler(media.on_look_callback, pattern=r"^lk")
    )
    app.add_handler(                                                   # group 2
        MessageHandler(
            filters.ChatType.GROUPS & ~filters.COMMAND, giveaway.on_group_message
        )
    )

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    main()
