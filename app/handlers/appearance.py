from __future__ import annotations

from string import Formatter

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app.context import AppContext
from app.handlers.helpers import answer_or_replace
from app.keyboards import button
from app.product_icons import extract_product_icon, product_emoji_parts
from app.services.ui_settings import (
    reset_all_ui_settings,
    reset_button_override,
    reset_text_override,
    save_button_override,
    save_text_override,
    save_ui_option,
)
from app.states import AppearanceStates
from app.texts import (
    TEXTS,
    get_default_text_template,
    get_text_override,
    get_text_template,
    text_template_placeholders,
)
from app.ui_customization import (
    BUTTON_DEFINITIONS,
    BUTTON_ORDER,
    UI_OPTION_DEFAULTS,
    ButtonOverride,
    button_definition,
    get_button_override,
    get_ui_option,
    render_custom_emoji,
    resolve_button,
)
from app.utils import h, h_truncate, shorten

router = Router(name="appearance")

_BUTTONS_PER_PAGE = 8
_TEXTS_PER_PAGE = 8

_BUTTON_TEXT_KEYS = {
    "menu_store",
    "menu_wallet",
    "menu_settings",
    "menu_support",
    "menu_history",
    "menu_language",
    "menu_admin",
    "back_menu",
    "back",
    "refresh",
    "buy",
    "sold_out",
    "confirm",
    "binance_pay",
    "copy_pay_id",
    "where_order_id",
    "verify_again",
    "cancel",
    "cancel_transaction",
    "contact_support",
}
_CUSTOMIZABLE_TEXT_KEYS = tuple(key for key in TEXTS["es"] if key not in _BUTTON_TEXT_KEYS)

_SAMPLE_VALUES: dict[str, object] = {
    "store": "Shop Ultra",
    "balance": "25.00",
    "name": "Producto de ejemplo",
    "description": "Descripción de ejemplo",
    "stock": "10",
    "price": "1.50",
    "order": "ORD-123456",
    "payload": "usuario@example.com | contraseña",
    "date": "2026-07-15 12:00 UTC",
    "tiers": "• $50.00+ → +2%",
    "minimum": "1.00",
    "pay_id": "123456789",
    "pay_name": "Nombre Binance",
    "amount": "20.00",
    "bonus": "1.00",
    "total": "21.00",
    "seconds": "10",
    "user": "@cliente",
    "telegram_id": "123456789",
    "language": "Español",
    "items": "• Elemento de ejemplo",
    "emoji": "🛍️",
    "added": "10",
    "available": "25",
    "count": "3",
}

_OPTION_LABELS = {
    "animated_menu_preview": "Vista animada en el menú",
    "animated_store_preview": "Vista animada en la tienda",
    "custom_emoji_buttons": "Emoji Premium en botones",
}


def _require_admin(event: Message | CallbackQuery, ctx: AppContext) -> bool:
    return event.from_user.id in ctx.config.admin_ids


async def _deny(event: Message | CallbackQuery) -> None:
    if isinstance(event, CallbackQuery):
        await event.answer("Acceso denegado", show_alert=True)
    else:
        await event.answer("Acceso denegado")


async def _guard(event: Message | CallbackQuery, ctx: AppContext) -> bool:
    if _require_admin(event, ctx):
        return True
    await _deny(event)
    return False


def _home_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [button("🎛 Botones y colores", callback_data="appearance:buttons:0", style="primary")],
            [
                button(
                    "🧾 Mensajes y apartados",
                    callback_data="appearance:text_languages",
                    style="primary",
                )
            ],
            [
                button(
                    "✨ Opciones de animación", callback_data="appearance:options", style="success"
                )
            ],
            [button("🧪 Probar emoji Premium", callback_data="appearance:test", style="success")],
            [
                button(
                    "♻️ Restaurar apariencia",
                    callback_data="appearance:reset_confirm",
                    style="danger",
                )
            ],
            [button("❌ Volver", callback_data="admin:home", style="danger")],
        ]
    )


async def _show_home(target: Message | CallbackQuery) -> None:
    text = (
        "🎨 <b>Apariencia y emojis Premium</b>\n\n"
        "Desde aquí puedes cambiar los textos, emojis e identidad visual del bot sin editar "
        "archivos ni reiniciar.\n\n"
        "Telegram permite cuatro estilos de botón: <b>azul</b>, <b>verde</b>, "
        "<b>rojo</b> y <b>predeterminado</b>. No permite colores HEX arbitrarios.\n\n"
        "Los emojis personalizados se muestran como icono oficial del botón. Además, el bot "
        "los repite como entidades animadas dentro del mensaje para que el movimiento sea "
        "visible incluso cuando una versión de Telegram dibuja el icono del botón estático."
    )
    await answer_or_replace(target, text, _home_keyboard())


@router.callback_query(F.data == "admin:appearance")
@router.callback_query(F.data == "appearance:home")
async def appearance_home(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    if not await _guard(callback, ctx):
        return
    await callback.answer()
    await state.clear()
    await _show_home(callback)


def _button_list_keyboard(page: int) -> InlineKeyboardMarkup:
    page = max(0, page)
    start = page * _BUTTONS_PER_PAGE
    keys = BUTTON_ORDER[start : start + _BUTTONS_PER_PAGE]
    rows = [
        [
            button(
                f"🎨 {BUTTON_DEFINITIONS[key].title_es}",
                callback_data=f"appearance:button:{key}:{page}",
                style="primary",
            )
        ]
        for key in keys
    ]
    nav = []
    if page > 0:
        nav.append(button("⬅️", callback_data=f"appearance:buttons:{page - 1}", style="primary"))
    nav.append(
        button(
            f"{page + 1}/{max(1, (len(BUTTON_ORDER) + _BUTTONS_PER_PAGE - 1) // _BUTTONS_PER_PAGE)}",
            callback_data="noop",
        )
    )
    if start + _BUTTONS_PER_PAGE < len(BUTTON_ORDER):
        nav.append(button("➡️", callback_data=f"appearance:buttons:{page + 1}", style="primary"))
    rows.append(nav)
    rows.append([button("❌ Volver", callback_data="appearance:home", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("appearance:buttons:"))
async def appearance_buttons(callback: CallbackQuery, ctx: AppContext) -> None:
    if not await _guard(callback, ctx):
        return
    page = int(callback.data.rsplit(":", 1)[1])
    await callback.answer()
    await answer_or_replace(
        callback,
        "🎛 <b>Botones y colores</b>\n\nSelecciona el botón que quieres modificar.",
        _button_list_keyboard(page),
    )


def _button_detail_keyboard(key: str, page: int, language: str = "es") -> InlineKeyboardMarkup:
    presentation = resolve_button(key, language, price="1.50", order="ORD-123", product="Producto")
    rows = [
        [
            button(
                presentation.text,
                callback_data="noop",
                style=presentation.style,
                icon_custom_emoji_id=presentation.icon_custom_emoji_id,
            )
        ],
        [
            button(
                "✏️ Texto ES", callback_data=f"appearance:label:es:{key}:{page}", style="primary"
            ),
            button(
                "✏️ Texto EN", callback_data=f"appearance:label:en:{key}:{page}", style="primary"
            ),
        ],
        [button("✨ Emoji", callback_data=f"appearance:emoji:{key}:{page}", style="primary")],
        [button("🎨 Color", callback_data=f"appearance:styles:{key}:{page}", style="primary")],
        [
            button(
                "♻️ Restablecer botón",
                callback_data=f"appearance:button_reset:{key}:{page}",
                style="danger",
            )
        ],
        [button("❌ Volver", callback_data=f"appearance:buttons:{page}", style="danger")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _show_button_detail(target: Message | CallbackQuery, key: str, page: int) -> None:
    definition = button_definition(key)
    override = get_button_override(key)
    current_es = resolve_button(key, "es", price="1.50", order="ORD-123", product="Producto")
    current_en = resolve_button(key, "en", price="1.50", order="ORD-123", product="Product")
    raw_icon = override.icon if override and override.icon is not None else definition.icon
    fallback, custom_id = product_emoji_parts(raw_icon) if raw_icon else ("sin icono", None)
    style = override.style if override and override.style is not None else definition.style
    icon_preview = render_custom_emoji(raw_icon)
    text = (
        f"🎛 <b>{h(definition.title_es)}</b>\n\n"
        f"Texto ES: <code>{h(current_es.label)}</code>\n"
        f"Texto EN: <code>{h(current_en.label)}</code>\n"
        f"Color: <b>{h(style)}</b>\n"
        f"Emoji: {icon_preview or h(fallback)}\n"
        f"ID Premium: <code>{h(custom_id or 'no configurado')}</code>\n\n"
        "La primera fila de botones es una vista previa real."
    )
    await answer_or_replace(target, text, _button_detail_keyboard(key, page))


@router.callback_query(F.data.startswith("appearance:button:"))
async def appearance_button_detail(callback: CallbackQuery, ctx: AppContext) -> None:
    if not await _guard(callback, ctx):
        return
    _, _, key, page_raw = callback.data.split(":", 3)
    if key not in BUTTON_DEFINITIONS:
        await callback.answer("Botón inválido", show_alert=True)
        return
    await callback.answer()
    await _show_button_detail(callback, key, int(page_raw))


@router.callback_query(F.data.startswith("appearance:label:"))
async def appearance_label_start(
    callback: CallbackQuery, state: FSMContext, ctx: AppContext
) -> None:
    if not await _guard(callback, ctx):
        return
    _, _, language, key, page_raw = callback.data.split(":", 4)
    if language not in {"es", "en"} or key not in BUTTON_DEFINITIONS:
        await callback.answer("Configuración inválida", show_alert=True)
        return
    definition = button_definition(key)
    template = definition.label_en if language == "en" else definition.label_es
    placeholders = text_template_placeholders(template)
    placeholder_hint = ", ".join(f"{{{name}}}" for name in sorted(placeholders)) or "ninguna"
    await state.set_state(AppearanceStates.waiting_button_label)
    await state.update_data(
        appearance_button_key=key, appearance_language=language, appearance_page=int(page_raw)
    )
    await callback.answer()
    await answer_or_replace(
        callback,
        f"✏️ <b>Texto del botón ({language.upper()})</b>\n\n"
        f"Envía el nuevo texto sin emoji. Variables permitidas: <code>{h(placeholder_hint)}</code>.\n"
        "Envía <code>-</code> para recuperar el texto predeterminado.",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    button(
                        "❌ Cancelar",
                        callback_data=f"appearance:button:{key}:{page_raw}",
                        style="danger",
                    )
                ]
            ]
        ),
    )


@router.message(AppearanceStates.waiting_button_label)
async def appearance_label_value(message: Message, state: FSMContext, ctx: AppContext) -> None:
    if not await _guard(message, ctx):
        return
    data = await state.get_data()
    key = str(data["appearance_button_key"])
    language = str(data["appearance_language"])
    page = int(data["appearance_page"])
    value = (message.text or "").strip()
    definition = button_definition(key)
    default_template = definition.label_en if language == "en" else definition.label_es
    allowed = text_template_placeholders(default_template)
    if value == "-":
        value_override: str | None = None
    else:
        if not value or len(value) > 90:
            await message.answer("El texto debe tener entre 1 y 90 caracteres.")
            return
        try:
            fields = _strict_template_fields(value)
        except ValueError as exc:
            await message.answer(f"❌ {h(exc)}")
            return
        unknown = fields - allowed
        if unknown:
            await message.answer(
                "Variables no permitidas: "
                + ", ".join(f"<code>{{{h(name)}}}</code>" for name in sorted(unknown))
            )
            return
        sample = value.format(**_SAMPLE_VALUES)
        if len(sample) > 96:
            await message.answer("El texto final del botón es demasiado largo.")
            return
        value_override = value

    current = get_button_override(key) or ButtonOverride()
    updated = ButtonOverride(
        label_es=value_override if language == "es" else current.label_es,
        label_en=value_override if language == "en" else current.label_en,
        icon=current.icon,
        style=current.style,
    )
    async with ctx.session_factory() as session:
        await save_button_override(session, key, updated)
    await state.clear()
    await message.answer("✅ Texto del botón actualizado.")
    await _show_button_detail(message, key, page)


@router.callback_query(F.data.startswith("appearance:emoji:"))
async def appearance_emoji_start(
    callback: CallbackQuery, state: FSMContext, ctx: AppContext
) -> None:
    if not await _guard(callback, ctx):
        return
    _, _, key, page_raw = callback.data.split(":", 3)
    if key not in BUTTON_DEFINITIONS:
        await callback.answer("Botón inválido", show_alert=True)
        return
    await state.set_state(AppearanceStates.waiting_button_emoji)
    await state.update_data(appearance_button_key=key, appearance_page=int(page_raw))
    await callback.answer()
    await answer_or_replace(
        callback,
        "✨ <b>Emoji del botón</b>\n\n"
        "Envía directamente un emoji personalizado de Telegram Premium o un emoji normal.\n"
        "También puedes enviar el sticker de tipo <i>custom emoji</i>.\n\n"
        "Un sticker normal, animado o de video no puede insertarse dentro de un botón; "
        "sí puede usarse como media al abrir un producto.\n\n"
        "Envía <code>-</code> para dejar el botón sin icono.",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    button(
                        "❌ Cancelar",
                        callback_data=f"appearance:button:{key}:{page_raw}",
                        style="danger",
                    )
                ]
            ]
        ),
    )


@router.message(AppearanceStates.waiting_button_emoji)
async def appearance_emoji_value(message: Message, state: FSMContext, ctx: AppContext) -> None:
    if not await _guard(message, ctx):
        return
    data = await state.get_data()
    key = str(data["appearance_button_key"])
    page = int(data["appearance_page"])
    if (message.text or "").strip() == "-":
        icon_value = ""
    else:
        try:
            selection = extract_product_icon(message)
        except ValueError as exc:
            await message.answer(f"❌ {h(exc)}")
            return
        if selection.media_type and selection.custom_emoji_id is None:
            await message.answer(
                "❌ Ese archivo es un sticker normal. Para el botón envía un emoji Premium "
                "desde el selector de emojis, no desde el selector de stickers."
            )
            return
        icon_value = selection.value

    current = get_button_override(key) or ButtonOverride()
    updated = ButtonOverride(
        label_es=current.label_es,
        label_en=current.label_en,
        icon=icon_value,
        style=current.style,
    )
    async with ctx.session_factory() as session:
        await save_button_override(session, key, updated)
    await state.clear()
    await message.answer("✅ Emoji del botón actualizado.")
    await _show_button_detail(message, key, page)


@router.callback_query(F.data.startswith("appearance:styles:"))
async def appearance_styles(callback: CallbackQuery, ctx: AppContext) -> None:
    if not await _guard(callback, ctx):
        return
    _, _, key, page_raw = callback.data.split(":", 3)
    if key not in BUTTON_DEFINITIONS:
        await callback.answer("Botón inválido", show_alert=True)
        return
    rows = [
        [
            button(
                "🔵 Azul",
                callback_data=f"appearance:style:primary:{key}:{page_raw}",
                style="primary",
            )
        ],
        [
            button(
                "🟢 Verde",
                callback_data=f"appearance:style:success:{key}:{page_raw}",
                style="success",
            )
        ],
        [
            button(
                "🔴 Rojo", callback_data=f"appearance:style:danger:{key}:{page_raw}", style="danger"
            )
        ],
        [button("⚪ Predeterminado", callback_data=f"appearance:style:default:{key}:{page_raw}")],
        [button("❌ Volver", callback_data=f"appearance:button:{key}:{page_raw}", style="danger")],
    ]
    await callback.answer()
    await answer_or_replace(
        callback,
        "🎨 <b>Color del botón</b>\n\nTelegram limita los botones a estos cuatro estilos.",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("appearance:style:"))
async def appearance_style_set(callback: CallbackQuery, ctx: AppContext) -> None:
    if not await _guard(callback, ctx):
        return
    _, _, style, key, page_raw = callback.data.split(":", 4)
    if key not in BUTTON_DEFINITIONS or style not in {"primary", "success", "danger", "default"}:
        await callback.answer("Configuración inválida", show_alert=True)
        return
    current = get_button_override(key) or ButtonOverride()
    updated = ButtonOverride(
        label_es=current.label_es,
        label_en=current.label_en,
        icon=current.icon,
        style=style,
    )
    async with ctx.session_factory() as session:
        await save_button_override(session, key, updated)
    await callback.answer("Color actualizado")
    await _show_button_detail(callback, key, int(page_raw))


@router.callback_query(F.data.startswith("appearance:button_reset:"))
async def appearance_button_reset(callback: CallbackQuery, ctx: AppContext) -> None:
    if not await _guard(callback, ctx):
        return
    _, _, key, page_raw = callback.data.split(":", 3)
    if key not in BUTTON_DEFINITIONS:
        await callback.answer("Botón inválido", show_alert=True)
        return
    async with ctx.session_factory() as session:
        await reset_button_override(session, key)
    await callback.answer("Botón restaurado")
    await _show_button_detail(callback, key, int(page_raw))


@router.callback_query(F.data == "appearance:text_languages")
async def appearance_text_languages(callback: CallbackQuery, ctx: AppContext) -> None:
    if not await _guard(callback, ctx):
        return
    await callback.answer()
    await answer_or_replace(
        callback,
        "🧾 <b>Mensajes y apartados</b>\n\nSelecciona el idioma que quieres personalizar.",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [button("🇪🇸 Español", callback_data="appearance:texts:es:0", style="success")],
                [button("🇺🇸 English", callback_data="appearance:texts:en:0", style="primary")],
                [button("❌ Volver", callback_data="appearance:home", style="danger")],
            ]
        ),
    )


def _human_text_key(key: str) -> str:
    return key.replace("_", " ").strip().capitalize()


def _text_list_keyboard(language: str, page: int) -> InlineKeyboardMarkup:
    page = max(0, page)
    start = page * _TEXTS_PER_PAGE
    keys = _CUSTOMIZABLE_TEXT_KEYS[start : start + _TEXTS_PER_PAGE]
    rows = []
    for index, key in enumerate(keys, start=start):
        marker = "✏️" if get_text_override(language, key) is not None else "🧾"
        rows.append(
            [
                button(
                    f"{marker} {shorten(_human_text_key(key), 38)}",
                    callback_data=f"appearance:text:{language}:{index}:{page}",
                    style="primary",
                )
            ]
        )
    nav = []
    if page > 0:
        nav.append(
            button("⬅️", callback_data=f"appearance:texts:{language}:{page - 1}", style="primary")
        )
    nav.append(
        button(
            f"{page + 1}/{max(1, (len(_CUSTOMIZABLE_TEXT_KEYS) + _TEXTS_PER_PAGE - 1) // _TEXTS_PER_PAGE)}",
            callback_data="noop",
        )
    )
    if start + _TEXTS_PER_PAGE < len(_CUSTOMIZABLE_TEXT_KEYS):
        nav.append(
            button("➡️", callback_data=f"appearance:texts:{language}:{page + 1}", style="primary")
        )
    rows.append(nav)
    rows.append([button("❌ Volver", callback_data="appearance:text_languages", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("appearance:texts:"))
async def appearance_texts(callback: CallbackQuery, ctx: AppContext) -> None:
    if not await _guard(callback, ctx):
        return
    _, _, language, page_raw = callback.data.split(":", 3)
    if language not in {"es", "en"}:
        await callback.answer("Idioma inválido", show_alert=True)
        return
    await callback.answer()
    await answer_or_replace(
        callback,
        f"🧾 <b>Mensajes {language.upper()}</b>\n\n"
        "Selecciona un apartado. Los marcados con ✏️ tienen una personalización guardada.",
        _text_list_keyboard(language, int(page_raw)),
    )


def _text_ref(index: int) -> str:
    if index < 0 or index >= len(_CUSTOMIZABLE_TEXT_KEYS):
        raise IndexError(index)
    return _CUSTOMIZABLE_TEXT_KEYS[index]


async def _show_text_detail(
    target: Message | CallbackQuery,
    language: str,
    index: int,
    page: int,
) -> None:
    key = _text_ref(index)
    current = get_text_template(language, key)
    default = get_default_text_template(language, key)
    placeholders = text_template_placeholders(default)
    placeholder_text = ", ".join(f"{{{name}}}" for name in sorted(placeholders)) or "ninguna"
    customized = get_text_override(language, key) is not None
    text = (
        f"🧾 <b>{h(_human_text_key(key))}</b>\n\n"
        f"Clave interna: <code>{h(key)}</code>\n"
        f"Idioma: <b>{language.upper()}</b>\n"
        f"Personalizado: <b>{'sí' if customized else 'no'}</b>\n"
        f"Variables disponibles: <code>{h(placeholder_text)}</code>\n\n"
        f"<b>Plantilla actual:</b>\n<pre>{h_truncate(current, 2600)}</pre>"
    )
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                button(
                    "✏️ Editar mensaje",
                    callback_data=f"appearance:text_edit:{language}:{index}:{page}",
                    style="primary",
                )
            ],
            [
                button(
                    "👁 Vista previa",
                    callback_data=f"appearance:text_preview:{language}:{index}:{page}",
                    style="success",
                )
            ],
            [
                button(
                    "♻️ Restaurar",
                    callback_data=f"appearance:text_reset:{language}:{index}:{page}",
                    style="danger",
                )
            ],
            [
                button(
                    "❌ Volver", callback_data=f"appearance:texts:{language}:{page}", style="danger"
                )
            ],
        ]
    )
    await answer_or_replace(target, text, markup)


@router.callback_query(F.data.startswith("appearance:text:"))
async def appearance_text_detail(callback: CallbackQuery, ctx: AppContext) -> None:
    if not await _guard(callback, ctx):
        return
    _, _, language, index_raw, page_raw = callback.data.split(":", 4)
    try:
        index = int(index_raw)
        _text_ref(index)
    except (ValueError, IndexError):
        await callback.answer("Mensaje inválido", show_alert=True)
        return
    await callback.answer()
    await _show_text_detail(callback, language, index, int(page_raw))


@router.callback_query(F.data.startswith("appearance:text_edit:"))
async def appearance_text_edit(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    if not await _guard(callback, ctx):
        return
    _, _, language, index_raw, page_raw = callback.data.split(":", 4)
    try:
        index = int(index_raw)
        key = _text_ref(index)
    except (ValueError, IndexError):
        await callback.answer("Mensaje inválido", show_alert=True)
        return
    default = get_default_text_template(language, key)
    placeholders = text_template_placeholders(default)
    placeholder_text = ", ".join(f"{{{name}}}" for name in sorted(placeholders)) or "ninguna"
    await state.set_state(AppearanceStates.waiting_text_template)
    await state.update_data(
        appearance_text_language=language,
        appearance_text_index=index,
        appearance_text_page=int(page_raw),
    )
    await callback.answer()
    await answer_or_replace(
        callback,
        "✏️ <b>Editar mensaje</b>\n\n"
        "Envía el mensaje completo. Puedes usar negrita, cursiva, enlaces y emojis Premium "
        "animados directamente desde Telegram.\n\n"
        f"Variables permitidas: <code>{h(placeholder_text)}</code>. Puedes omitir variables, "
        "pero no inventar nombres nuevos.\n\n"
        "Envía <code>-</code> para restaurar el texto original.",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    button(
                        "❌ Cancelar",
                        callback_data=f"appearance:text:{language}:{index}:{page_raw}",
                        style="danger",
                    )
                ]
            ]
        ),
    )


@router.message(AppearanceStates.waiting_text_template)
async def appearance_text_value(message: Message, state: FSMContext, ctx: AppContext) -> None:
    if not await _guard(message, ctx):
        return
    data = await state.get_data()
    language = str(data["appearance_text_language"])
    index = int(data["appearance_text_index"])
    page = int(data["appearance_text_page"])
    key = _text_ref(index)
    raw_text = (message.text or "").strip()
    if raw_text == "-":
        async with ctx.session_factory() as session:
            await reset_text_override(session, language, key)
        await state.clear()
        await message.answer("✅ Mensaje restaurado.")
        await _show_text_detail(message, language, index, page)
        return

    template = (message.html_text or message.text or "").strip()
    if not template or len(template) > 3900:
        await message.answer("El mensaje debe tener entre 1 y 3900 caracteres.")
        return
    allowed = text_template_placeholders(get_default_text_template(language, key))
    try:
        fields = _strict_template_fields(template)
    except ValueError as exc:
        await message.answer(f"❌ {h(exc)}")
        return
    unknown = fields - allowed
    if unknown:
        await message.answer(
            "Variables no permitidas: "
            + ", ".join(f"<code>{{{h(name)}}}</code>" for name in sorted(unknown))
        )
        return
    try:
        preview = template.format(**_SAMPLE_VALUES)
    except (KeyError, ValueError, IndexError) as exc:
        await message.answer(f"❌ La plantilla no se puede procesar: <code>{h(exc)}</code>")
        return
    if len(preview) > 4096:
        await message.answer("La vista previa supera el límite de 4096 caracteres de Telegram.")
        return

    try:
        await message.answer("👁 <b>Vista previa:</b>\n\n" + preview)
    except TelegramBadRequest as exc:
        await message.answer(
            "❌ Telegram rechazó el formato del mensaje. Revisa etiquetas, enlaces o emojis.\n\n"
            f"<code>{h(exc)}</code>"
        )
        return

    async with ctx.session_factory() as session:
        await save_text_override(session, language, key, template)
    await state.clear()
    await message.answer("✅ Mensaje actualizado y activado.")
    await _show_text_detail(message, language, index, page)


@router.callback_query(F.data.startswith("appearance:text_preview:"))
async def appearance_text_preview(callback: CallbackQuery, ctx: AppContext) -> None:
    if not await _guard(callback, ctx):
        return
    _, _, language, index_raw, page_raw = callback.data.split(":", 4)
    try:
        index = int(index_raw)
        key = _text_ref(index)
        preview = get_text_template(language, key).format(**_SAMPLE_VALUES)
    except (ValueError, IndexError, KeyError):
        await callback.answer("No se pudo generar la vista previa", show_alert=True)
        return
    await callback.answer()
    if callback.message is not None:
        await callback.message.answer("👁 <b>Vista previa:</b>\n\n" + preview)
    await _show_text_detail(callback, language, index, int(page_raw))


@router.callback_query(F.data.startswith("appearance:text_reset:"))
async def appearance_text_reset(callback: CallbackQuery, ctx: AppContext) -> None:
    if not await _guard(callback, ctx):
        return
    _, _, language, index_raw, page_raw = callback.data.split(":", 4)
    try:
        index = int(index_raw)
        key = _text_ref(index)
    except (ValueError, IndexError):
        await callback.answer("Mensaje inválido", show_alert=True)
        return
    async with ctx.session_factory() as session:
        await reset_text_override(session, language, key)
    await callback.answer("Mensaje restaurado")
    await _show_text_detail(callback, language, index, int(page_raw))


def _options_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for key, label in _OPTION_LABELS.items():
        enabled = get_ui_option(key)
        rows.append(
            [
                button(
                    f"{'✅' if enabled else '⚫'} {label}",
                    callback_data=f"appearance:toggle:{key}",
                    style="success" if enabled else "primary",
                )
            ]
        )
    rows.append([button("❌ Volver", callback_data="appearance:home", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _show_options(target: Message | CallbackQuery) -> None:
    await answer_or_replace(
        target,
        "✨ <b>Opciones de animación</b>\n\n"
        "La vista animada coloca los emojis Premium dentro del texto del mensaje, donde Telegram "
        "puede reproducirlos. Los iconos dentro de botones dependen de la versión y ajustes del cliente.",
        _options_keyboard(),
    )


@router.callback_query(F.data == "appearance:options")
async def appearance_options(callback: CallbackQuery, ctx: AppContext) -> None:
    if not await _guard(callback, ctx):
        return
    await callback.answer()
    await _show_options(callback)


@router.callback_query(F.data.startswith("appearance:toggle:"))
async def appearance_option_toggle(callback: CallbackQuery, ctx: AppContext) -> None:
    if not await _guard(callback, ctx):
        return
    key = callback.data.rsplit(":", 1)[1]
    if key not in UI_OPTION_DEFAULTS:
        await callback.answer("Opción inválida", show_alert=True)
        return
    enabled = not get_ui_option(key)
    async with ctx.session_factory() as session:
        await save_ui_option(session, key, enabled)
    await callback.answer("Opción actualizada")
    await _show_options(callback)


@router.callback_query(F.data == "appearance:test")
async def appearance_test_start(
    callback: CallbackQuery, state: FSMContext, ctx: AppContext
) -> None:
    if not await _guard(callback, ctx):
        return
    await state.set_state(AppearanceStates.waiting_test_emoji)
    await callback.answer()
    await answer_or_replace(
        callback,
        "🧪 <b>Prueba de emoji Premium</b>\n\n"
        "Envía un emoji personalizado animado. El bot probará el emoji dentro del mensaje y dentro "
        "de un botón real.",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [button("❌ Cancelar", callback_data="appearance:home", style="danger")]
            ]
        ),
    )


@router.message(AppearanceStates.waiting_test_emoji)
async def appearance_test_value(message: Message, state: FSMContext, ctx: AppContext) -> None:
    if not await _guard(message, ctx):
        return
    try:
        selection = extract_product_icon(message)
    except ValueError as exc:
        await message.answer(f"❌ {h(exc)}")
        return
    if selection.custom_emoji_id is None:
        await message.answer("Envía un emoji personalizado de Telegram Premium, no uno normal.")
        return
    fallback, custom_id = product_emoji_parts(selection.value)
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                button(
                    "Prueba dentro del botón",
                    callback_data="noop",
                    style="primary",
                    icon_custom_emoji_id=custom_id,
                )
            ]
        ]
    )
    try:
        await message.answer(
            "<b>Prueba dentro del mensaje:</b> " + render_custom_emoji(selection.value),
            reply_markup=markup,
        )
    except TelegramBadRequest as exc:
        await message.answer(
            "❌ Telegram rechazó el emoji para este bot. Comprueba que el propietario del bot "
            "tenga Telegram Premium y que el emoji siga disponible.\n\n"
            f"<code>{h(exc)}</code>"
        )
        return
    await state.clear()
    await message.answer(
        f"✅ Prueba enviada. Emoji de respaldo: {h(fallback)}.\n\n"
        "El movimiento dentro del texto es controlado por Telegram. En el botón, algunas versiones "
        "pueden mostrar un fotograma estático aunque el ID sea correcto."
    )
    await _show_home(message)


@router.callback_query(F.data == "appearance:reset_confirm")
async def appearance_reset_confirm(callback: CallbackQuery, ctx: AppContext) -> None:
    if not await _guard(callback, ctx):
        return
    await callback.answer()
    await answer_or_replace(
        callback,
        "⚠️ <b>Restaurar toda la apariencia</b>\n\n"
        "Esto eliminará textos, emojis y colores personalizados. No afecta productos, usuarios, "
        "saldos, compras, stock ni APIs.",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [button("✅ Sí, restaurar", callback_data="appearance:reset_all", style="danger")],
                [button("❌ Cancelar", callback_data="appearance:home", style="primary")],
            ]
        ),
    )


@router.callback_query(F.data == "appearance:reset_all")
async def appearance_reset_all(callback: CallbackQuery, ctx: AppContext) -> None:
    if not await _guard(callback, ctx):
        return
    async with ctx.session_factory() as session:
        await reset_all_ui_settings(session)
    await callback.answer("Apariencia restaurada")
    await _show_home(callback)


def _strict_template_fields(template: str) -> frozenset[str]:
    try:
        parsed = list(Formatter().parse(template))
    except ValueError as exc:
        raise ValueError("Las llaves de la plantilla no están balanceadas.") from exc

    fields: set[str] = set()
    for _literal, field_name, _format_spec, _conversion in parsed:
        if field_name is None:
            continue
        if not field_name.isidentifier():
            raise ValueError(
                "Usa variables simples como {price}; no se permiten atributos ni índices."
            )
        fields.add(field_name)
    return frozenset(fields)
