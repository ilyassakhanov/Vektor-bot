"""Telegram bot with long-polling.

Run with: python bot.py
"""

from __future__ import annotations

import logging
import os

import telebot

import config
from llm import LLM, LLMError, OllamaLLM

# Load secrets from .env into the environment (real env vars take precedence).
config.load_env()
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
log = logging.getLogger("vektor.bot")


def build_llm() -> LLM:
    """Composition root — pick the LLM provider from configuration."""
    return OllamaLLM(
        base_url=os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434"),
        model=os.environ.get("OLLAMA_MODEL", "llama3.2"),
    )


def load_allowed_usernames() -> frozenset[str]:
    """Parse ``ALLOWED_USERNAMES`` (comma-separated Telegram tags, e.g. ``@user``)."""
    raw = os.environ.get("ALLOWED_USERNAMES", "").strip()
    if not raw:
        return frozenset()
    return frozenset(
        part.strip().lstrip("@").lower()
        for part in raw.split(",")
        if part.strip()
    )


def handle_message(
    message: telebot.types.Message,
    llm: LLM,
    reply_to,
    allowed_usernames: set[str] | frozenset[str] | None = None,
) -> None:
    """Process a single message: call the LLM and reply via ``reply_to``.

    ``reply_to`` is the callable used to send a reply (typically
    ``bot.reply_to``); tests inject a fake.

    When ``allowed_usernames`` is provided, only users whose Telegram username
    is in that set may use the bot; others get a denial reply.
    """
    user = message.from_user
    log.info(
        "message id=%s chat=%s user=%s%s text=%r",
        message.message_id,
        message.chat.id,
        user.id if user else "?",
        f" @{user.username}" if user and user.username else "",
        message.text,
    )
    if allowed_usernames is not None and (
        user is None or not user.username
        or user.username.lower() not in allowed_usernames
    ):
        log.warning("unauthorized user=%s denied", user.id if user else "?")
        reply_to(message, "Sorry, you are not allowed to use this bot.")
        return
    try:
        response = llm.generate(message.text or "")
    except LLMError as exc:
        log.warning("LLM error: %s", exc)
        reply_to(message, "Sorry, I couldn't generate a response.")
        return
    reply_to(message, response.text)


def create_bot(llm: LLM, allowed_usernames: frozenset[str] | None = None) -> telebot.TeleBot:
    """Wire up a TeleBot with the injected LLM provider."""
    bot = telebot.TeleBot(BOT_TOKEN)

    @bot.message_handler(func=lambda m: True)
    def on_message(message: telebot.types.Message) -> None:
        handle_message(message, llm, bot.reply_to, allowed_usernames)

    return bot


def main() -> None:
    llm = build_llm()
    allowed = load_allowed_usernames()
    if allowed:
        log.info("Allowed users: %d", len(allowed))
    else:
        log.warning("No ALLOWED_USERNAMES set — all users denied.")
    bot = create_bot(llm, allowed)
    log.info("Starting bot (polling)...")
    try:
        bot.infinity_polling()
    except KeyboardInterrupt:
        log.info("Stopped by user.")
    finally:
        bot.stop_polling()
        log.info("Bot shut down.")


if __name__ == "__main__":
    main()
