from __future__ import annotations

from decimal import Decimal

from aiogram.types import CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup

from app.product_icons import product_emoji_parts
from app.services.catalog import ProductWithStock
from app.ui_customization import resolve_button
from app.utils import money, shorten


def button(
    text: str,
    *,
    callback_data: str | None = None,
    style: str | None = None,
    url: str | None = None,
    copy_text: str | None = None,
    icon_custom_emoji_id: str | None = None,
) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=text,
        callback_data=callback_data,
        style=style,
        url=url,
        copy_text=CopyTextButton(text=copy_text) if copy_text is not None else None,
        icon_custom_emoji_id=icon_custom_emoji_id,
    )


def appearance_button(
    appearance_key: str,
    language: str,
    *,
    callback_data: str | None = None,
    url: str | None = None,
    copy_text: str | None = None,
    **values: object,
) -> InlineKeyboardButton:
    presentation = resolve_button(appearance_key, language, **values)
    return button(
        presentation.text,
        callback_data=callback_data,
        style=presentation.style,
        url=url,
        copy_text=copy_text,
        icon_custom_emoji_id=presentation.icon_custom_emoji_id,
    )


def without_custom_emoji_icons(
    markup: InlineKeyboardMarkup | None,
) -> InlineKeyboardMarkup | None:
    if markup is None:
        return None
    rows = [
        [item.model_copy(update={"icon_custom_emoji_id": None}) for item in row]
        for row in markup.inline_keyboard
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def main_menu(language: str, *, is_admin: bool) -> InlineKeyboardMarkup:
    rows = [
        [appearance_button("main_store", language, callback_data="shop:0")],
        [
            appearance_button("main_wallet", language, callback_data="wallet"),
            appearance_button("main_settings", language, callback_data="settings"),
        ],
        [
            appearance_button("main_support", language, callback_data="support"),
            appearance_button("main_history", language, callback_data="history"),
        ],
        [appearance_button("main_language", language, callback_data="language")],
    ]
    if is_admin:
        rows.append([appearance_button("main_admin", language, callback_data="admin:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def store_keyboard(
    language: str,
    products: list[ProductWithStock],
    *,
    page: int = 0,
    total: int | None = None,
    page_size: int | None = None,
) -> InlineKeyboardMarkup:
    """Build one continuous storefront list without pagination controls.

    ``page``, ``total`` and ``page_size`` remain accepted so callbacks created
    by older releases continue to work after an in-place update. Every product
    button now points back to catalog page zero because the public catalog no
    longer has pages.
    """

    del page, total, page_size
    rows: list[list[InlineKeyboardButton]] = []
    for item in products:
        product = item.product
        fallback, custom_emoji_id = product_emoji_parts(product.button_emoji)
        prefix = "" if custom_emoji_id else f"{fallback} "
        stock_label = item.stock_text(language)
        stock_icon = "🟢" if item.available else "🔴"
        label = (
            f"{prefix}{shorten(product.name, 28)} | ${money(product.price)} | "
            f"{stock_icon} {stock_label}"
        )
        raw_style = product.button_style or "primary"
        style = None if raw_style == "default" else raw_style
        if style not in {None, "primary", "success", "danger"}:
            style = "primary"
        rows.append(
            [
                button(
                    label,
                    callback_data=f"product:{product.id}:0",
                    style=style,
                    icon_custom_emoji_id=custom_emoji_id,
                )
            ]
        )
    rows.append([appearance_button("nav_back_menu", language, callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def product_keyboard(
    language: str, *, product_id: int, page: int, price: Decimal, stock: int
) -> InlineKeyboardMarkup:
    if stock > 0:
        purchase_button = appearance_button(
            "product_buy",
            language,
            callback_data=f"buy:{product_id}:{page}",
            price=money(price),
        )
    else:
        purchase_button = appearance_button("product_sold_out", language, callback_data="noop")
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [purchase_button],
            [
                appearance_button(
                    "product_refresh",
                    language,
                    callback_data=f"product:{product_id}:{page}",
                )
            ],
            [
                appearance_button(
                    "nav_back",
                    language,
                    callback_data=f"shop:{page}",
                )
            ],
        ]
    )


def quantity_selector_keyboard(
    language: str,
    *,
    product_id: int,
    page: int,
    quantity: int,
    max_quantity: int,
) -> InlineKeyboardMarkup:
    quantity = max(1, min(int(quantity), max(1, int(max_quantity))))
    maximum = max(1, int(max_quantity))
    decrease_callback = f"buyqty:{product_id}:{quantity - 1}:{page}" if quantity > 1 else "noop"
    increase_callback = (
        f"buyqty:{product_id}:{quantity + 1}:{page}" if quantity < maximum else "noop"
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                button("➖", callback_data=decrease_callback, style="danger"),
                button(f"🤝 {quantity}", callback_data="noop", style="success"),
                button("➕ 1", callback_data=increase_callback, style="success"),
            ],
            [
                appearance_button(
                    "product_quantity_buy",
                    language,
                    callback_data=f"buyexecute:{product_id}:{quantity}:{page}",
                    quantity=quantity,
                )
            ],
            [
                appearance_button(
                    "product_refresh",
                    language,
                    callback_data=f"buyqty:{product_id}:{quantity}:{page}",
                ),
                appearance_button(
                    "product_quantity_custom",
                    language,
                    callback_data=f"buyqtycustom:{product_id}:{page}",
                ),
            ],
            [
                appearance_button(
                    "nav_back",
                    language,
                    callback_data=f"product:{product_id}:{page}",
                )
            ],
        ]
    )


def purchase_confirmation_keyboard(
    language: str, *, product_id: int, page: int, quantity: int = 1
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                appearance_button(
                    "product_confirm",
                    language,
                    callback_data=f"buyconfirm:{product_id}:{page}:{quantity}",
                )
            ],
            [
                appearance_button(
                    "nav_back",
                    language,
                    callback_data=f"buy:{product_id}:{page}",
                )
            ],
        ]
    )


def external_purchase_cancel_keyboard(
    language: str, *, product_id: int, page: int
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                appearance_button(
                    "nav_back",
                    language,
                    callback_data=f"product:{product_id}:{page}",
                )
            ]
        ]
    )


def slot_duration_keyboard(
    language: str,
    *,
    product_id: int,
    page: int,
    durations: tuple[int, ...],
) -> InlineKeyboardMarkup:
    values = tuple(dict.fromkeys(value for value in durations if value > 0)) or (1,)
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for months in values:
        label = f"{months} mes" if months == 1 and language == "es" else f"{months} meses"
        if language == "en":
            label = f"{months} month" if months == 1 else f"{months} months"
        row.append(
            button(
                label,
                callback_data=f"buymonths:{product_id}:{months}:{page}",
                style="primary",
            )
        )
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(
        [
            appearance_button(
                "nav_back",
                language,
                callback_data=f"product:{product_id}:{page}",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def wallet_keyboard(language: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [appearance_button("wallet_binance", language, callback_data="wallet:binance")],
            [appearance_button("nav_back_menu", language, callback_data="menu")],
        ]
    )


def payment_keyboard(language: str, pay_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [appearance_button("wallet_copy_pay_id", language, copy_text=pay_id)],
            [
                appearance_button(
                    "wallet_order_help",
                    language,
                    callback_data="wallet:order_help",
                )
            ],
            [appearance_button("wallet_cancel", language, callback_data="wallet:cancel")],
        ]
    )


def retry_deposit_keyboard(language: str, deposit_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                appearance_button(
                    "wallet_verify",
                    language,
                    callback_data=f"wallet:retry:{deposit_id}",
                )
            ],
            [
                appearance_button(
                    "wallet_cancel",
                    language,
                    callback_data=f"wallet:cancel_deposit:{deposit_id}",
                )
            ],
        ]
    )


def invalid_payment_keyboard(language: str, deposit_id: int | None) -> InlineKeyboardMarkup:
    cancel_callback = (
        f"wallet:cancel_deposit:{deposit_id}" if deposit_id is not None else "wallet:cancel"
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                appearance_button(
                    "wallet_order_help",
                    language,
                    callback_data="wallet:order_help",
                )
            ],
            [appearance_button("wallet_cancel", language, callback_data=cancel_callback)],
        ]
    )


def cancel_keyboard(language: str, callback_data: str = "cancel") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [appearance_button("action_cancel", language, callback_data=callback_data)]
        ]
    )


def simple_back(language: str, callback_data: str = "menu") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[appearance_button("nav_back", language, callback_data=callback_data)]]
    )


def settings_keyboard(language: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [appearance_button("settings_activity", language, callback_data="settings:activity")],
            [appearance_button("nav_back_menu", language, callback_data="menu")],
        ]
    )


def settings_activity_keyboard(language: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [appearance_button("nav_back", language, callback_data="settings")],
            [appearance_button("nav_back_menu", language, callback_data="menu")],
        ]
    )


def history_keyboard(
    language: str,
    orders: list[tuple[int, str, str]],
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for order_id, order_code, product_name in orders:
        rows.append(
            [
                appearance_button(
                    "history_resend",
                    language,
                    callback_data=f"history:order:{order_id}",
                    order=shorten(order_code, 18),
                    product=shorten(product_name, 20),
                )
            ]
        )
    rows.append([appearance_button("nav_back_menu", language, callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def language_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [appearance_button("language_es", "es", callback_data="setlang:es")],
            [appearance_button("language_en", "en", callback_data="setlang:en")],
            [appearance_button("nav_back", "es", callback_data="menu")],
        ]
    )
