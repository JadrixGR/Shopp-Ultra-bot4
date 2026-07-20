from __future__ import annotations

import logging

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app.context import AppContext
from app.keyboards import main_menu, without_custom_emoji_icons
from app.services.settings import get_store_profile
from app.services.users import get_or_create_user
from app.texts import t
from app.ui_customization import render_main_menu_animated_preview, strip_custom_emoji_entities
from app.utils import h, money

logger = logging.getLogger(__name__)


def _is_custom_emoji_error(exc: TelegramBadRequest) -> bool:
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "custom emoji",
            "custom_emoji",
            "icon_custom_emoji",
            "button_type_invalid",
            "button type invalid",
        )
    )


async def _answer_with_fallback(
    message: Message,
    text: str,
    reply_markup: InlineKeyboardMarkup | None,
) -> Message:
    try:
        return await message.answer(text, reply_markup=reply_markup)
    except TelegramBadRequest as exc:
        if not _is_custom_emoji_error(exc):
            raise
        logger.warning("Telegram rejected custom emoji; using Unicode fallback: %s", exc)
        return await message.answer(
            strip_custom_emoji_entities(text),
            reply_markup=without_custom_emoji_icons(reply_markup),
        )


async def answer_or_replace(
    target: Message | CallbackQuery,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> Message | None:
    if isinstance(target, Message):
        return await _answer_with_fallback(target, text, reply_markup)

    message = target.message
    if message is None:
        await target.answer()
        return None
    try:
        if message.text is not None:
            try:
                await message.edit_text(text, reply_markup=reply_markup)
            except TelegramBadRequest as exc:
                if not _is_custom_emoji_error(exc):
                    raise
                logger.warning(
                    "Telegram rejected custom emoji while editing; using fallback: %s",
                    exc,
                )
                await message.edit_text(
                    strip_custom_emoji_entities(text),
                    reply_markup=without_custom_emoji_icons(reply_markup),
                )
            return message
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc).lower():
            return message
        logger.debug("Could not edit message; sending a new one: %s", exc)

    # Send the replacement first. If Telegram rejects the new markup, the
    # existing menu remains visible instead of disappearing from the chat.
    replacement = await _answer_with_fallback(message, text, reply_markup)
    try:
        await message.delete()
    except TelegramBadRequest:
        pass
    return replacement


async def show_main_menu(target: Message | CallbackQuery, ctx: AppContext) -> None:
    telegram_user = target.from_user
    async with ctx.session_factory() as session:
        user = await get_or_create_user(session, telegram_user)
        profile = await get_store_profile(session)
    if user.is_banned:
        await answer_or_replace(target, t(user.language, "banned"))
        return
    text = t(
        user.language,
        "welcome",
        store=h(profile.name),
        balance=money(user.balance),
    )
    animated_preview = render_main_menu_animated_preview(
        user.language,
        is_admin=telegram_user.id in ctx.config.admin_ids,
    )
    if animated_preview:
        text += "\n\n" + t(user.language, "menu_animated_preview_title") + "\n" + animated_preview
    await answer_or_replace(
        target,
        text,
        main_menu(user.language, is_admin=telegram_user.id in ctx.config.admin_ids),
    )
