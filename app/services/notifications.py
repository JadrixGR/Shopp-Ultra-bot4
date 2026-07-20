from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.keyboards import appearance_button, without_custom_emoji_icons
from app.models import User
from app.texts import t
from app.ui_customization import render_product_icon, strip_custom_emoji_entities
from app.utils import h_truncate, money

logger = logging.getLogger(__name__)

_SEND_DELAY_SECONDS = 0.05


@dataclass(frozen=True, slots=True)
class BroadcastResult:
    attempted: int
    sent: int
    blocked: int
    failed: int


@dataclass(frozen=True, slots=True)
class ProductNotice:
    product_id: int
    name: str
    price: Decimal
    button_emoji: str = "🛍️"


def _stock_notification_text(
    language: str,
    *,
    product_name: str,
    button_emoji: str,
    added: int,
    available: int,
    price: Decimal,
    is_new_product: bool,
) -> str:
    key = "notice_stock_new" if is_new_product else "notice_stock_update"
    return t(
        language,
        key,
        emoji=render_product_icon(button_emoji),
        name=h_truncate(product_name, 500),
        added=added,
        available=available,
        price=money(price),
    )


def _product_available_text(
    language: str,
    *,
    product_name: str,
    button_emoji: str,
    price: Decimal,
    restocked: bool,
) -> str:
    key = "notice_product_restocked" if restocked else "notice_product_new"
    return t(
        language,
        key,
        emoji=render_product_icon(button_emoji),
        name=h_truncate(product_name, 500),
        price=money(price),
    )


def _catalog_update_text(
    language: str,
    *,
    products: Sequence[ProductNotice],
    restocked: bool,
) -> str:
    visible = list(products[:12])
    lines = [
        (
            f"{render_product_icon(item.button_emoji)} "
            f"<b>{h_truncate(item.name, 220)}</b> — ${money(item.price)}"
        )
        for item in visible
    ]
    if len(products) > len(visible):
        lines.append(t(language, "notice_more_products", count=len(products) - len(visible)))
    return t(
        language,
        "notice_catalog_restocked" if restocked else "notice_catalog_new",
        items="\n".join(lines),
    )


def _stock_notification_keyboard(language: str, product_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                appearance_button(
                    "notice_open_product",
                    language,
                    callback_data=f"product:{product_id}:0",
                )
            ]
        ]
    )


def _store_keyboard(language: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[appearance_button("notice_open_store", language, callback_data="shop:0")]]
    )


def _has_custom_emoji(
    text: str,
    markup: InlineKeyboardMarkup | None,
) -> bool:
    if "<tg-emoji" in text:
        return True
    return bool(
        markup and any(item.icon_custom_emoji_id for row in markup.inline_keyboard for item in row)
    )


async def _send_with_retry(
    bot: Bot,
    *,
    telegram_id: int,
    text: str,
    markup: InlineKeyboardMarkup | None,
) -> str:
    """Return ``sent``, ``blocked`` or ``failed``."""

    for attempt in range(2):
        try:
            await bot.send_message(telegram_id, text, reply_markup=markup)
            return "sent"
        except TelegramRetryAfter as exc:
            if attempt == 0:
                await asyncio.sleep(float(exc.retry_after) + 0.25)
                continue
            logger.warning("Broadcast rate-limited for user %s", telegram_id)
            return "failed"
        except TelegramForbiddenError:
            return "blocked"
        except TelegramBadRequest as exc:
            lowered = str(exc).lower()
            if "chat not found" in lowered or "user is deactivated" in lowered:
                return "blocked"
            if _has_custom_emoji(text, markup):
                try:
                    await bot.send_message(
                        telegram_id,
                        strip_custom_emoji_entities(text),
                        reply_markup=without_custom_emoji_icons(markup),
                    )
                    return "sent"
                except TelegramForbiddenError:
                    return "blocked"
                except TelegramBadRequest as fallback_exc:
                    logger.warning(
                        "Could not notify user %s after custom emoji fallback: %s",
                        telegram_id,
                        fallback_exc,
                    )
                    return "failed"
            logger.warning("Could not notify user %s: %s", telegram_id, exc)
            return "failed"
        except Exception:
            logger.exception("Unexpected broadcast failure for user %s", telegram_id)
            return "failed"
    return "failed"


async def _recipients(
    session_factory: async_sessionmaker[AsyncSession],
) -> list[tuple[int, str]]:
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(User.telegram_id, User.language)
                .where(User.is_banned.is_(False))
                .order_by(User.id)
            )
        ).all()
    return [
        (int(telegram_id), language if language in {"es", "en"} else "es")
        for telegram_id, language in rows
    ]


async def _broadcast(
    bot: Bot,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    text_builder: Callable[[str], str],
    markup_builder: Callable[[str], InlineKeyboardMarkup | None],
) -> BroadcastResult:
    recipients = await _recipients(session_factory)
    sent = blocked = failed = 0
    for telegram_id, language in recipients:
        result = await _send_with_retry(
            bot,
            telegram_id=telegram_id,
            text=text_builder(language),
            markup=markup_builder(language),
        )
        if result == "sent":
            sent += 1
        elif result == "blocked":
            blocked += 1
        else:
            failed += 1
        await asyncio.sleep(_SEND_DELAY_SECONDS)

    return BroadcastResult(
        attempted=len(recipients),
        sent=sent,
        blocked=blocked,
        failed=failed,
    )


async def broadcast_stock_update(
    bot: Bot,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    product_id: int,
    product_name: str,
    price: Decimal,
    added: int,
    available: int,
    is_new_product: bool = False,
    button_emoji: str = "🛍️",
) -> BroadcastResult:
    """Notify every registered, non-banned user who can still receive bot messages."""

    if added <= 0:
        return BroadcastResult(attempted=0, sent=0, blocked=0, failed=0)
    return await _broadcast(
        bot,
        session_factory,
        text_builder=lambda language: _stock_notification_text(
            language,
            product_name=product_name,
            button_emoji=button_emoji,
            added=added,
            available=available,
            price=price,
            is_new_product=is_new_product,
        ),
        markup_builder=lambda language: _stock_notification_keyboard(language, product_id),
    )


async def broadcast_product_available(
    bot: Bot,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    product_id: int,
    product_name: str,
    price: Decimal,
    restocked: bool = False,
    button_emoji: str = "🛍️",
) -> BroadcastResult:
    return await _broadcast(
        bot,
        session_factory,
        text_builder=lambda language: _product_available_text(
            language,
            product_name=product_name,
            button_emoji=button_emoji,
            price=price,
            restocked=restocked,
        ),
        markup_builder=lambda language: _stock_notification_keyboard(language, product_id),
    )


async def broadcast_catalog_update(
    bot: Bot,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    products: Sequence[ProductNotice],
    restocked: bool = False,
) -> BroadcastResult:
    if not products:
        return BroadcastResult(attempted=0, sent=0, blocked=0, failed=0)
    if len(products) == 1:
        item = products[0]
        return await broadcast_product_available(
            bot,
            session_factory,
            product_id=item.product_id,
            product_name=item.name,
            price=item.price,
            restocked=restocked,
            button_emoji=item.button_emoji,
        )
    return await _broadcast(
        bot,
        session_factory,
        text_builder=lambda language: _catalog_update_text(
            language,
            products=products,
            restocked=restocked,
        ),
        markup_builder=_store_keyboard,
    )


async def broadcast_announcement(
    bot: Bot,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    announcement_html: str,
) -> BroadcastResult:
    return await _broadcast(
        bot,
        session_factory,
        text_builder=lambda language: f"{t(language, 'announcement_title')}\n\n{announcement_html}",
        markup_builder=_store_keyboard,
    )
