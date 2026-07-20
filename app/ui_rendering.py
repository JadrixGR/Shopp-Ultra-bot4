from __future__ import annotations

from collections.abc import Sequence

from app.product_icons import product_emoji_parts
from app.services.catalog import ProductWithStock
from app.ui_customization import get_ui_option, render_custom_emoji
from app.utils import h_truncate, money, shorten

_STORE_PREVIEW_MAX_CHARS = 2800


def render_store_animated_preview(
    products: Sequence[ProductWithStock],
    language: str,
) -> str:
    """Render the visible catalog with animated custom emoji entities.

    Telegram clients may render a custom emoji button icon as a static frame. A
    custom emoji entity inside message text is therefore included as the animated
    representation. The preview is omitted when none of the visible products uses
    a custom emoji or when the administrator disables the option.
    """

    if not get_ui_option("animated_store_preview") or not products:
        return ""

    has_custom = any(
        product_emoji_parts(item.product.button_emoji)[1] is not None for item in products
    )
    if not has_custom:
        return ""

    lines: list[str] = []
    rendered_length = 0
    for item in products:
        product = item.product
        icon = render_custom_emoji(product.button_emoji)
        name = h_truncate(shorten(product.name, 44), 220)
        stock_marker = "🟢" if item.available else "🔴"
        stock = h_truncate(item.stock_text(language), 80)
        line = f"{icon} <b>{name}</b> — <b>${money(product.price)}</b> · {stock_marker} {stock}"
        separator_length = 1 if lines else 0
        if rendered_length + separator_length + len(line) > _STORE_PREVIEW_MAX_CHARS:
            lines.append("…")
            break
        lines.append(line)
        rendered_length += separator_length + len(line)
    return "\n".join(lines)
