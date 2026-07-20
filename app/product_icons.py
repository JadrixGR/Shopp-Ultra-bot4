from __future__ import annotations

from dataclasses import dataclass

from aiogram.enums import MessageEntityType
from aiogram.types import Message

CUSTOM_EMOJI_PREFIX = "ce:"
PRODUCT_EMOJI_MAX_LENGTH = 32
DEFAULT_PRODUCT_EMOJI = "🛍️"


@dataclass(frozen=True, slots=True)
class ProductIconSelection:
    value: str
    fallback: str
    custom_emoji_id: str | None = None
    media_type: str | None = None
    media_file_id: str | None = None


def pack_product_emoji(custom_emoji_id: str, fallback: str = "✨") -> str:
    custom_emoji_id = custom_emoji_id.strip()
    if not custom_emoji_id.isdigit():
        raise ValueError("ID de emoji personalizado inválido.")

    fallback = fallback.strip() or "✨"
    prefix = f"{CUSTOM_EMOJI_PREFIX}{custom_emoji_id}:"
    if len(prefix) >= PRODUCT_EMOJI_MAX_LENGTH:
        raise ValueError("El ID del emoji personalizado es demasiado largo.")
    if len(prefix) + len(fallback) > PRODUCT_EMOJI_MAX_LENGTH:
        fallback = "✨"
    return f"{prefix}{fallback}"


def product_emoji_parts(value: str | None) -> tuple[str, str | None]:
    raw = (value or "").strip()
    if raw.startswith(CUSTOM_EMOJI_PREFIX):
        parts = raw.split(":", 2)
        if len(parts) == 3 and parts[1].isdigit():
            return parts[2] or "✨", parts[1]
    return raw or DEFAULT_PRODUCT_EMOJI, None


def extract_product_icon(message: Message) -> ProductIconSelection:
    """Extract a normal/custom emoji or sticker from an administrator message.

    Custom emoji entities and custom-emoji stickers become button icons. Regular,
    animated and video stickers are stored as product media while their associated
    Unicode emoji is used as the button fallback.
    """

    text = (message.text or "").strip()
    for entity in message.entities or []:
        if entity.type == MessageEntityType.CUSTOM_EMOJI and entity.custom_emoji_id:
            fallback = entity.extract_from(message.text or "").strip() or "✨"
            value = pack_product_emoji(entity.custom_emoji_id, fallback)
            return ProductIconSelection(
                value=value,
                fallback=fallback,
                custom_emoji_id=entity.custom_emoji_id,
            )

    sticker = message.sticker
    if sticker is not None:
        fallback = (sticker.emoji or DEFAULT_PRODUCT_EMOJI).strip()
        custom_emoji_id = getattr(sticker, "custom_emoji_id", None)
        sticker_type = str(getattr(sticker, "type", ""))
        if custom_emoji_id or sticker_type.endswith("custom_emoji"):
            if not custom_emoji_id:
                raise ValueError("Telegram no entregó el ID de este emoji personalizado.")
            return ProductIconSelection(
                value=pack_product_emoji(custom_emoji_id, fallback),
                fallback=fallback,
                custom_emoji_id=custom_emoji_id,
                media_type="sticker",
                media_file_id=sticker.file_id,
            )
        return ProductIconSelection(
            value=fallback,
            fallback=fallback,
            media_type="sticker",
            media_file_id=sticker.file_id,
        )

    if text == "-" or not text:
        return ProductIconSelection(
            value=DEFAULT_PRODUCT_EMOJI,
            fallback=DEFAULT_PRODUCT_EMOJI,
        )
    if len(text) > PRODUCT_EMOJI_MAX_LENGTH:
        raise ValueError("El emoji o texto es demasiado largo.")
    return ProductIconSelection(value=text, fallback=text)
