from __future__ import annotations

import html
import json
import re
from dataclasses import asdict, dataclass
from string import Formatter
from typing import Final

from app.product_icons import product_emoji_parts

VALID_BUTTON_STYLES: Final[tuple[str, ...]] = ("primary", "success", "danger", "default")


@dataclass(frozen=True, slots=True)
class ButtonDefinition:
    key: str
    title_es: str
    title_en: str
    label_es: str
    label_en: str
    icon: str
    style: str


@dataclass(frozen=True, slots=True)
class ButtonOverride:
    label_es: str | None = None
    label_en: str | None = None
    icon: str | None = None
    style: str | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str) -> ButtonOverride:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("Invalid button appearance payload")
        style = payload.get("style")
        if style is not None and style not in VALID_BUTTON_STYLES:
            style = None
        return cls(
            label_es=_optional_string(payload.get("label_es")),
            label_en=_optional_string(payload.get("label_en")),
            icon=_optional_string(payload.get("icon"), preserve_empty=True),
            style=style,
        )


@dataclass(frozen=True, slots=True)
class ButtonPresentation:
    text: str
    style: str | None
    icon_custom_emoji_id: str | None
    fallback_icon: str
    label: str


BUTTON_DEFINITIONS: Final[dict[str, ButtonDefinition]] = {
    # Main menu
    "main_store": ButtonDefinition(
        "main_store", "Tienda", "Store", "Tienda", "Store", "🎯", "success"
    ),
    "main_wallet": ButtonDefinition(
        "main_wallet",
        "Recargar Wallet",
        "Top Up Wallet",
        "Recargar Wallet",
        "Top Up Wallet",
        "💰",
        "primary",
    ),
    "main_settings": ButtonDefinition(
        "main_settings", "Ajustes", "Settings", "Ajustes", "Settings", "⚡", "primary"
    ),
    "main_support": ButtonDefinition(
        "main_support", "Soporte", "Support", "Soporte", "Support", "🔔", "danger"
    ),
    "main_history": ButtonDefinition(
        "main_history", "Historial", "History", "Historial", "History", "🧿", "primary"
    ),
    "main_language": ButtonDefinition(
        "main_language", "Lenguaje", "Language", "Lenguaje", "Language", "🌐", "success"
    ),
    "main_admin": ButtonDefinition(
        "main_admin",
        "Administración",
        "Administration",
        "Administración",
        "Administration",
        "🛠️",
        "primary",
    ),
    # Store and purchase flow
    "product_buy": ButtonDefinition(
        "product_buy", "Comprar", "Buy", "COMPRAR | ${price}", "BUY | ${price}", "🛒", "primary"
    ),
    "product_sold_out": ButtonDefinition(
        "product_sold_out", "Agotado", "Sold out", "AGOTADO", "SOLD OUT", "🔴", "danger"
    ),
    "product_refresh": ButtonDefinition(
        "product_refresh", "Actualizar", "Refresh", "Actualizar", "Refresh", "🔄", "success"
    ),
    "product_confirm": ButtonDefinition(
        "product_confirm",
        "Confirmar compra",
        "Confirm purchase",
        "Confirmar compra",
        "Confirm purchase",
        "✅",
        "success",
    ),
    "product_quantity_buy": ButtonDefinition(
        "product_quantity_buy",
        "Comprar cantidad",
        "Buy quantity",
        "COMPRAR x{quantity}",
        "BUY x{quantity}",
        "🛒",
        "success",
    ),
    "product_quantity_custom": ButtonDefinition(
        "product_quantity_custom",
        "Cantidad personalizada",
        "Custom quantity",
        "Cantidad personalizada",
        "Custom quantity",
        "➕",
        "primary",
    ),
    "history_resend": ButtonDefinition(
        "history_resend",
        "Reenviar entrega",
        "Resend delivery",
        "Reenviar {order} · {product}",
        "Resend {order} · {product}",
        "📦",
        "primary",
    ),
    # Wallet
    "wallet_binance": ButtonDefinition(
        "wallet_binance",
        "Depósito Binance Pay",
        "Binance Pay Deposit",
        "Depósito Binance Pay",
        "Binance Pay Deposit",
        "🟡",
        "success",
    ),
    "wallet_copy_pay_id": ButtonDefinition(
        "wallet_copy_pay_id",
        "Copiar Pay ID",
        "Copy Pay ID",
        "Copiar Pay ID",
        "Copy Pay ID",
        "📋",
        "primary",
    ),
    "wallet_order_help": ButtonDefinition(
        "wallet_order_help",
        "Ayuda con Order ID",
        "Order ID help",
        "¿Dónde encuentro el Order ID?",
        "Where is the Order ID?",
        "🆔",
        "primary",
    ),
    "wallet_verify": ButtonDefinition(
        "wallet_verify",
        "Verificar nuevamente",
        "Verify again",
        "Verificar nuevamente",
        "Verify again",
        "🔄",
        "success",
    ),
    "wallet_cancel": ButtonDefinition(
        "wallet_cancel",
        "Cancelar transacción",
        "Cancel transaction",
        "Cancelar transacción",
        "Cancel transaction",
        "❌",
        "danger",
    ),
    # Navigation and common actions
    "nav_back_menu": ButtonDefinition(
        "nav_back_menu",
        "Volver al menú",
        "Back to menu",
        "Volver al menú",
        "Back to menu",
        "❌",
        "danger",
    ),
    "nav_back": ButtonDefinition("nav_back", "Volver", "Back", "Volver", "Back", "❌", "danger"),
    "action_cancel": ButtonDefinition(
        "action_cancel", "Cancelar", "Cancel", "Cancelar", "Cancel", "❌", "danger"
    ),
    "support_contact": ButtonDefinition(
        "support_contact",
        "Contactar soporte",
        "Contact support",
        "Contactar soporte",
        "Contact support",
        "💬",
        "success",
    ),
    "settings_activity": ButtonDefinition(
        "settings_activity",
        "Mis recargas y compras",
        "My deposits and purchases",
        "Mis recargas",
        "My activity",
        "💰",
        "primary",
    ),
    "language_es": ButtonDefinition(
        "language_es", "Español", "Spanish", "Español", "Spanish", "🇪🇸", "success"
    ),
    "language_en": ButtonDefinition(
        "language_en", "Inglés", "English", "English", "English", "🇺🇸", "primary"
    ),
    "notice_open_product": ButtonDefinition(
        "notice_open_product",
        "Ver producto",
        "View product",
        "Ver producto",
        "View product",
        "🛍️",
        "success",
    ),
    "notice_open_store": ButtonDefinition(
        "notice_open_store",
        "Abrir tienda",
        "Open store",
        "Abrir tienda",
        "Open store",
        "🛍️",
        "success",
    ),
}

BUTTON_ORDER: Final[tuple[str, ...]] = tuple(BUTTON_DEFINITIONS)
MAIN_MENU_BUTTON_KEYS: Final[tuple[str, ...]] = (
    "main_store",
    "main_wallet",
    "main_settings",
    "main_support",
    "main_history",
    "main_language",
    "main_admin",
)

UI_OPTION_DEFAULTS: Final[dict[str, bool]] = {
    "animated_menu_preview": True,
    "animated_store_preview": True,
    "custom_emoji_buttons": True,
}

_BUTTON_OVERRIDES: dict[str, ButtonOverride] = {}
_UI_OPTIONS: dict[str, bool] = dict(UI_OPTION_DEFAULTS)

_CUSTOM_EMOJI_TAG_RE = re.compile(
    r'<tg-emoji\s+emoji-id="\d+">(?P<fallback>.*?)</tg-emoji>',
    flags=re.IGNORECASE | re.DOTALL,
)


def _optional_string(value: object, *, preserve_empty: bool = False) -> str | None:
    if value is None:
        return None
    text = str(value)
    if preserve_empty:
        return text
    text = text.strip()
    return text or None


def install_button_overrides(overrides: dict[str, ButtonOverride]) -> None:
    _BUTTON_OVERRIDES.clear()
    for key, value in overrides.items():
        if key in BUTTON_DEFINITIONS:
            _BUTTON_OVERRIDES[key] = value


def set_button_override_runtime(key: str, value: ButtonOverride | None) -> None:
    if key not in BUTTON_DEFINITIONS:
        raise KeyError(key)
    if value is None:
        _BUTTON_OVERRIDES.pop(key, None)
    else:
        _BUTTON_OVERRIDES[key] = value


def get_button_override(key: str) -> ButtonOverride | None:
    return _BUTTON_OVERRIDES.get(key)


def install_ui_options(options: dict[str, bool]) -> None:
    _UI_OPTIONS.clear()
    _UI_OPTIONS.update(UI_OPTION_DEFAULTS)
    for key, value in options.items():
        if key in UI_OPTION_DEFAULTS:
            _UI_OPTIONS[key] = bool(value)


def set_ui_option_runtime(key: str, enabled: bool) -> None:
    if key not in UI_OPTION_DEFAULTS:
        raise KeyError(key)
    _UI_OPTIONS[key] = bool(enabled)


def get_ui_option(key: str) -> bool:
    return _UI_OPTIONS.get(key, UI_OPTION_DEFAULTS.get(key, False))


def button_definition(key: str) -> ButtonDefinition:
    return BUTTON_DEFINITIONS[key]


def _format_label(template: str, values: dict[str, object]) -> str:
    try:
        return template.format(**values)
    except (KeyError, ValueError, IndexError):
        return template


def resolve_button(key: str, language: str, **values: object) -> ButtonPresentation:
    definition = button_definition(key)
    override = get_button_override(key)
    is_en = language == "en"
    template = (
        override.label_en
        if is_en and override and override.label_en is not None
        else override.label_es
        if not is_en and override and override.label_es is not None
        else definition.label_en
        if is_en
        else definition.label_es
    )
    label = _format_label(template, values).strip() or (
        definition.label_en if is_en else definition.label_es
    )

    raw_icon = override.icon if override and override.icon is not None else definition.icon
    fallback_icon = ""
    custom_emoji_id: str | None = None
    if raw_icon:
        fallback_icon, custom_emoji_id = product_emoji_parts(raw_icon)
    if not get_ui_option("custom_emoji_buttons"):
        custom_emoji_id = None

    if custom_emoji_id:
        text = label
    elif fallback_icon:
        text = f"{fallback_icon} {label}".strip()
    else:
        text = label

    raw_style = override.style if override and override.style is not None else definition.style
    style = None if raw_style == "default" else raw_style
    return ButtonPresentation(
        text=text,
        style=style,
        icon_custom_emoji_id=custom_emoji_id,
        fallback_icon=fallback_icon,
        label=label,
    )


def render_custom_emoji(value: str | None) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    fallback, custom_emoji_id = product_emoji_parts(raw)
    safe_fallback = html.escape(fallback or "✨")
    if custom_emoji_id:
        return f'<tg-emoji emoji-id="{custom_emoji_id}">{safe_fallback}</tg-emoji>'
    return safe_fallback


def strip_custom_emoji_entities(text: str) -> str:
    return _CUSTOM_EMOJI_TAG_RE.sub(lambda match: match.group("fallback"), text)


def render_main_menu_animated_preview(language: str, *, is_admin: bool) -> str:
    """Render menu custom emoji as message entities so Telegram can animate them.

    Button icons use ``icon_custom_emoji_id`` too, but animation inside a button is
    client-controlled. The text preview provides a reliable animated representation.
    """

    if not get_ui_option("animated_menu_preview"):
        return ""
    keys = list(MAIN_MENU_BUTTON_KEYS)
    if not is_admin:
        keys.remove("main_admin")
    rows: list[str] = []
    has_custom = False
    for key in keys:
        definition = button_definition(key)
        override = get_button_override(key)
        raw_icon = override.icon if override and override.icon is not None else definition.icon
        _fallback, custom_emoji_id = product_emoji_parts(raw_icon) if raw_icon else ("", None)
        has_custom = has_custom or custom_emoji_id is not None
        presentation = resolve_button(key, language)
        icon_html = render_custom_emoji(raw_icon)
        rows.append(f"{icon_html} {html.escape(presentation.label)}".strip())
    if not has_custom:
        return ""
    return " · ".join(rows)


def render_product_icon(value: str | None) -> str:
    return render_custom_emoji(value)


def template_placeholders(template: str) -> frozenset[str]:
    names: set[str] = set()
    for _literal, field_name, _format_spec, _conversion in Formatter().parse(template):
        if not field_name:
            continue
        root = field_name.split(".", 1)[0].split("[", 1)[0]
        names.add(root)
    return frozenset(names)


def normalize_button_style(value: str | None) -> str:
    candidate = (value or "default").strip().lower()
    if candidate not in VALID_BUTTON_STYLES:
        raise ValueError("Invalid Telegram button style")
    return candidate
