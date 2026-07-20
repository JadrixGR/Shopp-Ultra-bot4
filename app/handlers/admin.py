from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from decimal import Decimal

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy import func, select

from app.context import AppContext
from app.handlers.helpers import answer_or_replace
from app.keyboards import button
from app.models import Deposit, Order, Product, ProviderPurchase, Refund, StockItem, User
from app.product_icons import extract_product_icon, product_emoji_parts
from app.rich_text import capture_message_rich_text, ensure_html_block_before, render_rich_text
from app.services.catalog import (
    ProductWithStock,
    add_stock_items,
    create_product,
    get_product_with_stock,
    list_all_products,
)
from app.services.deposits import (
    DepositAlreadyProcessed,
    DuplicateTransaction,
    credit_deposit,
    reject_deposit,
)
from app.services.external_purchases import (
    ExternalOrderManualReview,
    ExternalOrderRejected,
    refund_provider_purchase,
    retry_provider_purchase,
)
from app.services.notifications import broadcast_product_available, broadcast_stock_update
from app.services.prodseller import ProdSellerError
from app.services.provider_catalog import (
    refresh_prodseller_product,
    sync_prodseller_catalog,
)
from app.services.settings import (
    BINANCE_PAY_ID,
    BINANCE_PAY_NAME,
    BONUS_TIERS,
    STORE_NAME,
    SUPPORT_USERNAME,
    get_store_profile,
    parse_bonus_tiers,
    set_runtime_setting,
)
from app.services.stock_import import StockImportError, extract_stock_payloads
from app.states import (
    AddProductStates,
    AddStockStates,
    EditProductStates,
    EditSettingStates,
)
from app.texts import t
from app.ui_customization import render_custom_emoji, strip_custom_emoji_entities
from app.utils import h, h_truncate, money, parse_money, shorten

logger = logging.getLogger(__name__)
router = Router(name="admin")


async def _preview_product_icon(message: Message, value: str) -> str:
    fallback, custom_emoji_id = product_emoji_parts(value)
    if custom_emoji_id is None:
        return value

    preview = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                button(
                    "Vista previa del producto",
                    callback_data="noop",
                    style="primary",
                    icon_custom_emoji_id=custom_emoji_id,
                )
            ]
        ]
    )
    try:
        await message.answer(
            "Vista previa animada dentro del mensaje: " + render_custom_emoji(value),
            reply_markup=preview,
        )
    except TelegramBadRequest as exc:
        logger.warning("Telegram rejected product custom emoji %s: %s", custom_emoji_id, exc)
        await message.answer(
            "⚠️ Telegram rechazó ese emoji personalizado para botones. "
            f"Se usará el emoji normal {h(fallback)}.",
        )
        return fallback
    return value


async def _require_admin(event: Message | CallbackQuery, ctx: AppContext) -> bool:
    if event.from_user.id in ctx.config.admin_ids:
        return True
    if isinstance(event, CallbackQuery):
        await event.answer("Acceso denegado", show_alert=True)
    else:
        await event.answer("Acceso denegado")
    return False


async def _broadcast_stock_and_report(
    *,
    bot: Bot,
    ctx: AppContext,
    admin_chat_id: int,
    product_id: int,
    product_name: str,
    price: Decimal,
    added: int,
    available: int,
    is_new_product: bool,
    button_emoji: str = "🛍️",
) -> None:
    result = await broadcast_stock_update(
        bot,
        ctx.session_factory,
        product_id=product_id,
        product_name=product_name,
        price=price,
        added=added,
        available=available,
        is_new_product=is_new_product,
        button_emoji=button_emoji,
    )
    try:
        await bot.send_message(
            admin_chat_id,
            "📣 <b>Notificación de stock terminada</b>\n\n"
            f"Usuarios revisados: <b>{result.attempted}</b>\n"
            f"Enviadas: <b>{result.sent}</b>\n"
            f"Bloqueadas/no disponibles: <b>{result.blocked}</b>\n"
            f"Fallidas: <b>{result.failed}</b>",
        )
    except Exception:
        logger.exception("Could not report stock broadcast result to admin %s", admin_chat_id)


async def _broadcast_product_and_report(
    *,
    bot: Bot,
    ctx: AppContext,
    admin_chat_id: int,
    product_id: int,
    product_name: str,
    price: Decimal,
    button_emoji: str = "🛍️",
) -> None:
    result = await broadcast_product_available(
        bot,
        ctx.session_factory,
        product_id=product_id,
        product_name=product_name,
        price=price,
        button_emoji=button_emoji,
    )
    try:
        await bot.send_message(
            admin_chat_id,
            "📣 <b>Notificación de producto terminada</b>\n\n"
            f"Usuarios revisados: <b>{result.attempted}</b>\n"
            f"Enviadas: <b>{result.sent}</b>\n"
            f"Bloqueadas/no disponibles: <b>{result.blocked}</b>\n"
            f"Fallidas: <b>{result.failed}</b>",
        )
    except Exception:
        logger.exception("Could not report product broadcast result to admin %s", admin_chat_id)


def _admin_home_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [button("📦 Productos", callback_data="admin:products", style="primary")],
            [button("➕ Agregar producto", callback_data="admin:add", style="success")],
            [button("💳 Depósitos", callback_data="admin:deposits", style="primary")],
            [button("🔌 Proveedores API", callback_data="admin:providers", style="success")],
            [button("📣 Anuncios", callback_data="admin:announcements", style="success")],
            [button("💸 Reembolsos y saldo", callback_data="admin:finance", style="primary")],
            [button("⚙️ Configuración", callback_data="admin:settings", style="primary")],
            [button("🎨 Apariencia y emojis", callback_data="admin:appearance", style="success")],
            [button("🏠 Menú de usuario", callback_data="menu", style="danger")],
        ]
    )


def _admin_cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[button("❌ Cancelar", callback_data="admin:cancel", style="danger")]]
    )


_ADMIN_PRODUCTS_PAGE_SIZE = 35


def _build_admin_products_page(
    products: list[ProductWithStock],
    *,
    page: int,
    provider_names: dict[str, str] | None = None,
) -> tuple[str, InlineKeyboardMarkup, int, int]:
    """Build a Telegram-safe, paginated administrator product list.

    The public storefront remains a single continuous list. Pagination is used
    only in the administrator panel because that panel also includes inactive
    and external products and can easily exceed Telegram's reply-markup size.
    Local products are shown first so stock management remains easy.
    """

    names = provider_names or {}
    ordered = sorted(
        products,
        key=lambda item: (item.product.is_external, -int(item.product.id)),
    )
    total = len(ordered)
    page_count = max(1, (total + _ADMIN_PRODUCTS_PAGE_SIZE - 1) // _ADMIN_PRODUCTS_PAGE_SIZE)
    normalized_page = max(0, min(int(page), page_count - 1))
    start = normalized_page * _ADMIN_PRODUCTS_PAGE_SIZE
    visible = ordered[start : start + _ADMIN_PRODUCTS_PAGE_SIZE]

    rows: list[list] = []
    for item in visible:
        product = item.product
        state_icon = "🟢" if product.active else "⚫"
        fallback, custom_emoji_id = product_emoji_parts(product.button_emoji)
        emoji_prefix = "" if custom_emoji_id else f"{fallback} "
        source = names.get(product.provider_code or "") or product.provider_code or "LOCAL"
        stock_label = item.stock_text("es")
        rows.append(
            [
                button(
                    f"{state_icon} {emoji_prefix}{shorten(product.name, 22)} | "
                    f"${money(product.price)} | {source} {stock_label}",
                    callback_data=f"admin:product:{product.id}:{normalized_page}",
                    style="primary" if product.active else None,
                    icon_custom_emoji_id=custom_emoji_id,
                )
            ]
        )

    if page_count > 1:
        navigation = []
        if normalized_page > 0:
            navigation.append(
                button(
                    "⬅️ Anterior",
                    callback_data=f"admin:products:{normalized_page - 1}",
                    style="primary",
                )
            )
        navigation.append(button(f"{normalized_page + 1}/{page_count}", callback_data="noop"))
        if normalized_page + 1 < page_count:
            navigation.append(
                button(
                    "Siguiente ➡️",
                    callback_data=f"admin:products:{normalized_page + 1}",
                    style="primary",
                )
            )
        rows.append(navigation)

    rows.extend(
        [
            [button("➕ Agregar", callback_data="admin:add", style="success")],
            [button("❌ Volver", callback_data="admin:home", style="danger")],
        ]
    )

    local_count = sum(not item.product.is_external for item in ordered)
    external_count = total - local_count
    text = "📦 <b>Productos</b>"
    if not ordered:
        text += "\n\nTodavía no hay productos."
    else:
        end = start + len(visible)
        text += (
            f"\n\nMostrando <b>{start + 1}-{end}</b> de <b>{total}</b>."
            f"\nPágina <b>{normalized_page + 1}/{page_count}</b>."
            f"\nStock local: <b>{local_count}</b> · API: <b>{external_count}</b>."
        )
    return text, InlineKeyboardMarkup(inline_keyboard=rows), normalized_page, page_count


async def _admin_stats(ctx: AppContext) -> str:
    async with ctx.session_factory() as session:
        users = int(await session.scalar(select(func.count(User.id))) or 0)
        products = int(
            await session.scalar(select(func.count(Product.id)).where(Product.active.is_(True)))
            or 0
        )
        stock = int(
            await session.scalar(
                select(func.count(StockItem.id)).where(StockItem.status == "available")
            )
            or 0
        )
        orders = int(
            await session.scalar(select(func.count(Order.id)).where(Order.status == "completed"))
            or 0
        )
        revenue = Decimal(
            await session.scalar(
                select(
                    func.coalesce(
                        func.sum(Order.price - func.coalesce(Order.refunded_amount, 0)),
                        0,
                    )
                ).where(Order.status == "completed")
            )
            or 0
        )
        refunded = Decimal(
            await session.scalar(select(func.coalesce(func.sum(Refund.amount), 0))) or 0
        )
        pending = int(
            await session.scalar(select(func.count(Deposit.id)).where(Deposit.status == "pending"))
            or 0
        )
        api_products = int(
            await session.scalar(
                select(func.count(Product.id)).where(Product.provider_code.is_not(None))
            )
            or 0
        )
        api_pending = int(
            await session.scalar(
                select(func.count(ProviderPurchase.id)).where(
                    ProviderPurchase.status.in_(
                        ["processing", "pending", "pending_delivery", "manual_review"]
                    )
                )
            )
            or 0
        )
    return (
        "🛠 <b>Panel de administración</b>\n\n"
        f"👥 Usuarios: <b>{users}</b>\n"
        f"📦 Productos activos: <b>{products}</b>\n"
        f"🧾 Stock local disponible: <b>{stock}</b>\n"
        f"🔌 Productos externos: <b>{api_products}</b>\n"
        f"🛍 Ventas: <b>{orders}</b>\n"
        f"💵 Ingresos netos: <b>${money(revenue)}</b>\n"
        f"💸 Reembolsado: <b>${money(refunded)}</b>\n"
        f"🕓 Depósitos pendientes: <b>{pending}</b>\n"
        f"⚠️ Pedidos API por revisar: <b>{api_pending}</b>"
    )


async def _show_admin_home(target: Message | CallbackQuery, ctx: AppContext) -> None:
    await answer_or_replace(target, await _admin_stats(ctx), _admin_home_keyboard())


@router.message(Command("admin"))
async def admin_command(message: Message, state: FSMContext, ctx: AppContext) -> None:
    if not await _require_admin(message, ctx):
        return
    await state.clear()
    await _show_admin_home(message, ctx)


@router.callback_query(F.data == "admin:home")
async def admin_home(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    if not await _require_admin(callback, ctx):
        return
    await callback.answer()
    await state.clear()
    await _show_admin_home(callback, ctx)


@router.callback_query(F.data == "admin:cancel")
async def admin_cancel(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    if not await _require_admin(callback, ctx):
        return
    await callback.answer("Operación cancelada")
    await state.clear()
    await _show_admin_home(callback, ctx)


@router.callback_query(F.data == "admin:products")
@router.callback_query(F.data.startswith("admin:products:"))
async def admin_products(callback: CallbackQuery, ctx: AppContext) -> None:
    if not await _require_admin(callback, ctx):
        return
    await callback.answer()
    page = 0
    if callback.data and callback.data.startswith("admin:products:"):
        try:
            page = int(callback.data.rsplit(":", 1)[1])
        except (TypeError, ValueError):
            page = 0
    async with ctx.session_factory() as session:
        products = await list_all_products(session)
    provider_names = {
        runtime.config.code: runtime.config.name for runtime in ctx.providers.values()
    }
    text, markup, _page, _page_count = _build_admin_products_page(
        products,
        page=page,
        provider_names=provider_names,
    )
    await answer_or_replace(callback, text, markup)


async def _show_admin_product(
    target: Message | CallbackQuery,
    ctx: AppContext,
    product_id: int,
    *,
    return_page: int = 0,
) -> None:
    async with ctx.session_factory() as session:
        item = await get_product_with_stock(session, product_id)
        sold = int(
            await session.scalar(
                select(func.count(StockItem.id)).where(
                    StockItem.product_id == product_id,
                    StockItem.status == "sold",
                )
            )
            or 0
        )
    if item is None:
        await answer_or_replace(target, "Producto no encontrado.", _admin_home_keyboard())
        return
    product = item.product
    fallback_emoji, custom_emoji_id = product_emoji_parts(product.button_emoji)
    emoji_kind = " <i>(animado)</i>" if custom_emoji_id else ""
    runtime = ctx.providers.get(product.provider_code)
    source = (
        runtime.config.name if runtime is not None else (product.provider_code or "Stock local")
    )
    text = (
        f"📦 <b>{h_truncate(product.name, 350)}</b>\n\n"
        f"ID local: <code>{product.id}</code>\n"
        f"Origen: <b>{h(source)}</b>\n"
        f"Precio de venta: <b>${money(product.price)}</b>\n"
        f"Emoji: {render_custom_emoji(product.button_emoji) if custom_emoji_id else h(fallback_emoji)}{emoji_kind}\n"
        f"Color del botón: <b>{h(product.button_style or 'primary')}</b>\n"
        f"Estado: <b>{'Activo' if product.active else 'Inactivo'}</b>\n"
        f"Disponibilidad: <b>{h(item.stock_text('es'))}</b>\n"
        f"Vendidos: <b>{sold}</b>\n"
        f"Media local: <b>{h(product.media_type or 'sin media')}</b>\n"
        f"Duración para prorrateo: <b>{product.service_days or 'sin configurar'}"
        f"{' días' if product.service_days else ''}</b>\n"
    )
    if product.is_external:
        synced = (
            product.provider_synced_at.strftime("%Y-%m-%d %H:%M UTC")
            if product.provider_synced_at
            else "nunca"
        )
        text += (
            f"ID proveedor: <code>{h(product.external_product_id or '')}</code>\n"
            f"Costo proveedor: <b>${money(product.provider_cost or 0)}</b>\n"
            f"Última sincronización: <b>{h(synced)}</b>\n"
        )
    text += "\n📝 <b>Descripción</b>\n" + render_rich_text(
        product.description,
        product.description_entities,
        max_chars=1800,
    )
    text += "\n\n📋 <b>Instrucciones</b>\n" + render_rich_text(
        product.instructions,
        product.instructions_entities,
        max_chars=1200,
    )

    toggle_label = "⏸ Desactivar" if product.active else "▶️ Activar"
    toggle_style = "danger" if product.active else "success"
    first_action = (
        button(
            "🔄 Actualizar desde proveedor",
            callback_data=f"admin:provider:refresh_product:{product.id}",
            style="success",
        )
        if product.is_external
        else button("➕ Agregar stock", callback_data=f"admin:stock:{product.id}", style="success")
    )
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [first_action],
            [
                button("✏️ Nombre", callback_data=f"admin:edit:name:{product.id}", style="primary"),
                button(
                    "💵 Precio", callback_data=f"admin:edit:price:{product.id}", style="primary"
                ),
            ],
            [
                button("🧩 Emoji", callback_data=f"admin:edit:emoji:{product.id}", style="primary"),
                button(
                    "📝 Descripción",
                    callback_data=f"admin:edit:description:{product.id}",
                    style="primary",
                ),
            ],
            [
                button(
                    "🎨 Color botón",
                    callback_data=f"admin:product_style:{product.id}",
                    style="primary",
                )
            ],
            [
                button(
                    "📋 Instrucciones",
                    callback_data=f"admin:edit:instructions:{product.id}",
                    style="primary",
                )
            ],
            [
                button(
                    "🖼 Foto/GIF/Sticker",
                    callback_data=f"admin:edit:media:{product.id}",
                    style="primary",
                )
            ],
            [
                button(
                    "⏱ Duración",
                    callback_data=f"admin:edit:duration:{product.id}",
                    style="primary",
                )
            ],
            [button(toggle_label, callback_data=f"admin:toggle:{product.id}", style=toggle_style)],
            [
                button(
                    "❌ Volver",
                    callback_data=f"admin:products:{max(0, int(return_page))}",
                    style="danger",
                )
            ],
        ]
    )
    await answer_or_replace(target, text, markup)


@router.callback_query(F.data.startswith("admin:product:"))
async def admin_product_detail(callback: CallbackQuery, ctx: AppContext) -> None:
    if not await _require_admin(callback, ctx):
        return
    parts = (callback.data or "").split(":")
    try:
        product_id = int(parts[2])
        return_page = int(parts[3]) if len(parts) > 3 else 0
    except (IndexError, ValueError):
        await callback.answer("Producto inválido", show_alert=True)
        return
    await callback.answer()
    await _show_admin_product(callback, ctx, product_id, return_page=return_page)


@router.callback_query(F.data.startswith("admin:toggle:"))
async def admin_toggle_product(callback: CallbackQuery, bot: Bot, ctx: AppContext) -> None:
    if not await _require_admin(callback, ctx):
        return
    product_id = int(callback.data.rsplit(":", 1)[1])
    async with ctx.session_factory() as session:
        product = await session.get(Product, product_id)
        if product is None:
            await callback.answer("Producto no encontrado", show_alert=True)
            return
        was_active = product.active
        product.active = not product.active
        await session.commit()
        item = await get_product_with_stock(session, product_id)
        name = product.name
        price = Decimal(product.price)
        button_emoji = product.button_emoji
        is_active = product.active
    await callback.answer("Estado actualizado")
    await _show_admin_product(callback, ctx, product_id)
    if not was_active and is_active and item is not None and item.stock > 0:
        ctx.spawn(
            _broadcast_product_and_report(
                bot=bot,
                ctx=ctx,
                admin_chat_id=callback.from_user.id,
                product_id=product_id,
                product_name=name,
                price=price,
                button_emoji=button_emoji,
            )
        )


@router.callback_query(F.data.startswith("admin:product_style:"))
async def admin_product_style(callback: CallbackQuery, ctx: AppContext) -> None:
    if not await _require_admin(callback, ctx):
        return
    product_id = int(callback.data.rsplit(":", 1)[1])
    async with ctx.session_factory() as session:
        product = await session.get(Product, product_id)
    if product is None:
        await callback.answer("Producto no encontrado", show_alert=True)
        return
    current = product.button_style or "primary"
    rows = [
        [
            button(
                "🔵 Azul",
                callback_data=f"admin:product_style_set:{product_id}:primary",
                style="primary",
            )
        ],
        [
            button(
                "🟢 Verde",
                callback_data=f"admin:product_style_set:{product_id}:success",
                style="success",
            )
        ],
        [
            button(
                "🔴 Rojo",
                callback_data=f"admin:product_style_set:{product_id}:danger",
                style="danger",
            )
        ],
        [
            button(
                "⚪ Predeterminado", callback_data=f"admin:product_style_set:{product_id}:default"
            )
        ],
        [button("❌ Volver", callback_data=f"admin:product:{product_id}", style="danger")],
    ]
    await callback.answer()
    await answer_or_replace(
        callback,
        f"🎨 <b>Color del botón del producto</b>\n\nActual: <b>{h(current)}</b>\n\n"
        "Telegram permite azul, verde, rojo o el estilo predeterminado.",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("admin:product_style_set:"))
async def admin_product_style_set(callback: CallbackQuery, ctx: AppContext) -> None:
    if not await _require_admin(callback, ctx):
        return
    parts = callback.data.split(":")
    if len(parts) != 5 or parts[4] not in {"primary", "success", "danger", "default"}:
        await callback.answer("Color inválido", show_alert=True)
        return
    product_id = int(parts[3])
    style = parts[4]
    async with ctx.session_factory() as session:
        product = await session.get(Product, product_id)
        if product is None:
            await callback.answer("Producto no encontrado", show_alert=True)
            return
        product.button_style = style
        await session.commit()
    await callback.answer("Color actualizado")
    await _show_admin_product(callback, ctx, product_id)


# ---------------------------- Add product wizard ----------------------------


@router.callback_query(F.data == "admin:add")
async def admin_add_start(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    if not await _require_admin(callback, ctx):
        return
    await callback.answer()
    await state.clear()
    await state.set_state(AddProductStates.waiting_name)
    await answer_or_replace(
        callback,
        "➕ <b>Nuevo producto</b>\n\nEnvía el nombre del producto.",
        _admin_cancel_keyboard(),
    )


@router.message(AddProductStates.waiting_name)
async def add_product_name(message: Message, state: FSMContext, ctx: AppContext) -> None:
    if not await _require_admin(message, ctx):
        return
    name = (message.text or "").strip()
    if not name or len(name) > 180:
        await message.answer("Nombre inválido. Máximo 180 caracteres.")
        return
    await state.update_data(name=name)
    await state.set_state(AddProductStates.waiting_price)
    await message.answer(
        "💵 Envía el precio en USDT. Ejemplo: <code>5.00</code>",
        reply_markup=_admin_cancel_keyboard(),
    )


@router.message(AddProductStates.waiting_price)
async def add_product_price(message: Message, state: FSMContext, ctx: AppContext) -> None:
    if not await _require_admin(message, ctx):
        return
    try:
        price = parse_money(message.text or "")
    except ValueError:
        price = Decimal("0")
    if price <= 0:
        await message.answer("Precio inválido.")
        return
    await state.update_data(price=str(price))
    await state.set_state(AddProductStates.waiting_emoji)
    await message.answer(
        "🧩 Envía un emoji normal o un emoji animado de Telegram Premium.\n"
        "El emoji aparecerá delante del nombre del producto.\n"
        "Envía <code>-</code> para usar 🛍️.",
        reply_markup=_admin_cancel_keyboard(),
    )


@router.message(AddProductStates.waiting_emoji)
async def add_product_emoji(message: Message, state: FSMContext, ctx: AppContext) -> None:
    if not await _require_admin(message, ctx):
        return
    try:
        selection = extract_product_icon(message)
        emoji = await _preview_product_icon(message, selection.value)
    except ValueError as exc:
        await message.answer(f"❌ {h(exc)}")
        return
    updates: dict[str, object] = {"emoji": emoji}
    if selection.media_type and selection.media_file_id:
        updates.update(
            media_type=selection.media_type,
            media_file_id=selection.media_file_id,
        )
        await message.answer(
            "✅ El sticker se guardó como media del producto y su emoji se usará en el botón."
        )
    await state.update_data(**updates)
    await state.set_state(AddProductStates.waiting_description)
    await message.answer(
        "📝 Envía la descripción del producto.",
        reply_markup=_admin_cancel_keyboard(),
    )


@router.message(AddProductStates.waiting_description)
async def add_product_description(message: Message, state: FSMContext, ctx: AppContext) -> None:
    if not await _require_admin(message, ctx):
        return
    description, description_entities = capture_message_rich_text(message)
    if not description or len(description) > 3000:
        await message.answer("Descripción inválida. Máximo 3000 caracteres.")
        return
    await state.update_data(
        description=description,
        description_entities=description_entities,
    )
    await state.set_state(AddProductStates.waiting_instructions)
    await message.answer(
        "📋 <b>Instrucciones opcionales</b>\n\n"
        "Envía las instrucciones que verá el cliente después de la descripción. "
        "Puedes usar varias líneas. Envía <code>-</code> para omitirlas.",
        reply_markup=_admin_cancel_keyboard(),
    )


async def _advance_to_media(message: Message, state: FSMContext) -> None:
    await state.set_state(AddProductStates.waiting_media)
    data = await state.get_data()
    has_preselected_media = bool(data.get("media_type") and data.get("media_file_id"))
    rows = []
    if has_preselected_media:
        rows.append(
            [
                button(
                    "✅ Conservar sticker enviado",
                    callback_data="admin:add:keep_media",
                    style="success",
                )
            ]
        )
    rows.extend(
        [
            [button("⏭ Sin media", callback_data="admin:add:skip_media", style="primary")],
            [button("❌ Cancelar", callback_data="admin:cancel", style="danger")],
        ]
    )
    await message.answer(
        "🖼 Envía una <b>foto</b>, <b>GIF/animación</b> o <b>sticker</b> "
        "para representar el producto.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.message(AddProductStates.waiting_instructions)
async def add_product_instructions(message: Message, state: FSMContext, ctx: AppContext) -> None:
    if not await _require_admin(message, ctx):
        return
    value, entities = capture_message_rich_text(message)
    if value == "-":
        value = ""
        entities = "[]"
    if len(value) > 3000:
        await message.answer("Las instrucciones no pueden superar 3000 caracteres.")
        return
    await state.update_data(instructions=value, instructions_entities=entities)
    await _advance_to_media(message, state)


async def _advance_to_stock(message: Message, state: FSMContext) -> None:
    await state.set_state(AddProductStates.waiting_stock)
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [button("⏭ Crear sin stock", callback_data="admin:add:skip_stock", style="primary")],
            [button("❌ Cancelar", callback_data="admin:cancel", style="danger")],
        ]
    )
    await message.answer(
        "📦 <b>Carga masiva de stock</b>\n\n"
        "Para valores simples puedes usar <b>un elemento por línea</b>.\n\n"
        "Para cuentas o entregas con varias líneas, separa cada unidad con una línea "
        "que contenga únicamente <code>--</code>. Ejemplo:\n"
        "<pre>correo1@ejemplo.com\nclave1\nnota opcional\n--\ncorreo2@ejemplo.com\nclave2</pre>"
        "También puedes adjuntar un archivo <code>.txt</code>, <code>.csv</code> o "
        "<code>.log</code>.\n\nMáximo: <b>20,000 elementos</b>. "
        "Los duplicados se omiten.",
        reply_markup=markup,
    )


@router.message(AddProductStates.waiting_media, F.photo)
async def add_product_photo(message: Message, state: FSMContext, ctx: AppContext) -> None:
    if not await _require_admin(message, ctx):
        return
    await state.update_data(media_type="photo", media_file_id=message.photo[-1].file_id)
    await _advance_to_stock(message, state)


@router.message(AddProductStates.waiting_media, F.animation)
async def add_product_animation(message: Message, state: FSMContext, ctx: AppContext) -> None:
    if not await _require_admin(message, ctx):
        return
    await state.update_data(
        media_type="animation",
        media_file_id=message.animation.file_id,
    )
    await _advance_to_stock(message, state)


@router.message(AddProductStates.waiting_media, F.sticker)
async def add_product_sticker(message: Message, state: FSMContext, ctx: AppContext) -> None:
    if not await _require_admin(message, ctx):
        return
    await state.update_data(media_type="sticker", media_file_id=message.sticker.file_id)
    await _advance_to_stock(message, state)


@router.message(AddProductStates.waiting_media)
async def add_product_media_invalid(message: Message, ctx: AppContext) -> None:
    if not await _require_admin(message, ctx):
        return
    await message.answer("Envía una foto, GIF/animación, sticker o pulsa “Sin media”.")


@router.callback_query(AddProductStates.waiting_media, F.data == "admin:add:keep_media")
async def add_product_keep_media(
    callback: CallbackQuery, state: FSMContext, ctx: AppContext
) -> None:
    if not await _require_admin(callback, ctx):
        return
    data = await state.get_data()
    if not data.get("media_type") or not data.get("media_file_id"):
        await callback.answer("No hay media seleccionada", show_alert=True)
        return
    await callback.answer()
    await _advance_to_stock(callback.message, state)


@router.callback_query(AddProductStates.waiting_media, F.data == "admin:add:skip_media")
async def add_product_skip_media(
    callback: CallbackQuery, state: FSMContext, ctx: AppContext
) -> None:
    if not await _require_admin(callback, ctx):
        return
    await callback.answer()
    await state.update_data(media_type=None, media_file_id=None)
    await _advance_to_stock(callback.message, state)


async def _show_product_confirmation(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    stock_items = data.get("stock_items") or []
    text = (
        "✅ <b>Confirma el producto</b>\n\n"
        f"Nombre: <b>{h(data['name'])}</b>\n"
        f"Precio: <b>${money(data['price'])}</b>\n"
        f"Emoji: {h(product_emoji_parts(data['emoji'])[0])}\n"
        f"Media: <b>{h(data.get('media_type') or 'sin media')}</b>\n"
        f"Stock inicial: <b>{len(stock_items)}</b>\n\n"
        "Descripción:\n"
        + render_rich_text(
            str(data["description"]),
            str(data.get("description_entities") or "[]"),
            max_chars=1800,
        )
    )
    instructions = str(data.get("instructions") or "").strip()
    if instructions:
        text += "\n\nInstrucciones:\n" + render_rich_text(
            instructions,
            str(data.get("instructions_entities") or "[]"),
            max_chars=1000,
        )
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [button("✅ Crear producto", callback_data="admin:add:confirm", style="success")],
            [button("❌ Cancelar", callback_data="admin:cancel", style="danger")],
        ]
    )
    await state.set_state(AddProductStates.waiting_confirmation)
    await message.answer(text, reply_markup=markup)


@router.message(AddProductStates.waiting_stock)
async def add_product_stock(
    message: Message,
    state: FSMContext,
    bot: Bot,
    ctx: AppContext,
) -> None:
    if not await _require_admin(message, ctx):
        return
    try:
        items = await extract_stock_payloads(message, bot)
    except StockImportError as exc:
        await message.answer(f"❌ {h(exc)}")
        return
    await state.update_data(stock_items=items)
    await message.answer(f"📥 Se detectaron <b>{len(items)}</b> elementos de stock.")
    await _show_product_confirmation(message, state)


@router.callback_query(AddProductStates.waiting_stock, F.data == "admin:add:skip_stock")
async def add_product_skip_stock(
    callback: CallbackQuery, state: FSMContext, ctx: AppContext
) -> None:
    if not await _require_admin(callback, ctx):
        return
    await callback.answer()
    await state.update_data(stock_items=[])
    await _show_product_confirmation(callback.message, state)


@router.callback_query(AddProductStates.waiting_confirmation, F.data == "admin:add:confirm")
async def add_product_confirm(
    callback: CallbackQuery,
    state: FSMContext,
    bot: Bot,
    ctx: AppContext,
) -> None:
    if not await _require_admin(callback, ctx):
        return
    data = await state.get_data()
    added = duplicates = 0
    async with ctx.session_factory() as session:
        product = await create_product(
            session,
            name=data["name"],
            description=data["description"],
            description_entities=str(data.get("description_entities") or "[]"),
            instructions=str(data.get("instructions") or ""),
            instructions_entities=str(data.get("instructions_entities") or "[]"),
            price=Decimal(data["price"]),
            button_emoji=data["emoji"],
            media_type=data.get("media_type"),
            media_file_id=data.get("media_file_id"),
        )
        items = list(data.get("stock_items") or [])
        if items:
            added, duplicates = await add_stock_items(session, product.id, items)
        item = await get_product_with_stock(session, product.id)

    await state.clear()
    await callback.answer("Producto creado")
    if callback.message is not None:
        await callback.message.answer(
            f"✅ Producto creado. Stock agregado: <b>{added}</b> · "
            f"Duplicados omitidos: <b>{duplicates}</b>."
        )
    await _show_admin_product(callback, ctx, product.id)

    if added > 0 and item is not None:
        ctx.spawn(
            _broadcast_stock_and_report(
                bot=bot,
                ctx=ctx,
                admin_chat_id=callback.from_user.id,
                product_id=product.id,
                product_name=product.name,
                price=Decimal(product.price),
                added=added,
                available=item.stock,
                is_new_product=True,
                button_emoji=product.button_emoji,
            )
        )


# ------------------------------- Add stock ---------------------------------


@router.callback_query(F.data.startswith("admin:stock:"))
async def admin_stock_start(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    if not await _require_admin(callback, ctx):
        return
    product_id = int(callback.data.rsplit(":", 1)[1])
    async with ctx.session_factory() as session:
        product = await session.get(Product, product_id)
    if product is None:
        await callback.answer("Producto no encontrado", show_alert=True)
        return
    if product.is_external:
        await callback.answer(
            "Este producto usa stock externo; sincronízalo desde Proveedores API.",
            show_alert=True,
        )
        return
    await callback.answer()
    await state.set_state(AddStockStates.waiting_items)
    await state.update_data(product_id=product_id)
    await answer_or_replace(
        callback,
        f"➕ <b>Agregar stock a {h(product.name)}</b>\n\n"
        "Para valores simples usa <b>uno por línea</b>. Para una cuenta o entrega "
        "de varias líneas, separa cada unidad con una línea que contenga únicamente "
        "<code>--</code>.\n\n"
        "Ejemplo:\n<pre>usuario1\nclave1\nnota\n--\nusuario2\nclave2</pre>"
        "También puedes adjuntar un archivo <code>.txt</code>, <code>.csv</code> o "
        "<code>.log</code>. Máximo: <b>20,000 elementos</b> por carga.",
        _admin_cancel_keyboard(),
    )


@router.message(AddStockStates.waiting_items)
async def admin_stock_receive(
    message: Message,
    state: FSMContext,
    bot: Bot,
    ctx: AppContext,
) -> None:
    if not await _require_admin(message, ctx):
        return
    data = await state.get_data()
    product_id = int(data["product_id"])
    try:
        items = await extract_stock_payloads(message, bot)
    except StockImportError as exc:
        await message.answer(f"❌ {h(exc)}")
        return

    async with ctx.session_factory() as session:
        product = await session.get(Product, product_id)
        if product is None:
            await state.clear()
            await message.answer("Producto no encontrado.")
            return
        added, duplicates = await add_stock_items(session, product_id, items)
        item = await get_product_with_stock(session, product_id)

    await state.clear()
    await message.answer(
        f"✅ Procesados: <b>{len(items)}</b> · Agregados: <b>{added}</b> · "
        f"Duplicados omitidos: <b>{duplicates}</b>"
    )
    await _show_admin_product(message, ctx, product_id)

    if added > 0 and item is not None and product.active:
        await message.answer(
            "📣 El stock quedó guardado. Las notificaciones a los clientes se están enviando."
        )
        ctx.spawn(
            _broadcast_stock_and_report(
                bot=bot,
                ctx=ctx,
                admin_chat_id=message.chat.id,
                product_id=product.id,
                product_name=product.name,
                price=Decimal(product.price),
                added=added,
                available=item.stock,
                is_new_product=False,
                button_emoji=product.button_emoji,
            )
        )
    elif added > 0 and not product.active:
        await message.answer(
            "ℹ️ No se notificó a los clientes porque el producto está desactivado. "
            "Actívalo antes de anunciarlo."
        )


# ------------------------------- Edit product ------------------------------


_EDIT_PROMPTS = {
    "name": "Envía el nuevo nombre.",
    "price": "Envía el nuevo precio en USDT.",
    "emoji": (
        "Envía un emoji normal o un emoji animado de Telegram Premium. "
        "Envía <code>-</code> para usar 🛍️."
    ),
    "description": "Envía la nueva descripción.",
    "instructions": "Envía las nuevas instrucciones o <code>-</code> para quitarlas.",
    "media": "Envía una foto, GIF/animación o sticker. Envía <code>-</code> para quitar la media.",
    "duration": (
        "Envía la duración del servicio en días para calcular prorrateos. "
        "Ejemplo: <code>30</code>. Envía <code>-</code> para dejarla sin configurar."
    ),
}


@router.callback_query(F.data.startswith("admin:edit:"))
async def admin_edit_start(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    if not await _require_admin(callback, ctx):
        return
    parts = callback.data.split(":")
    if len(parts) != 4 or parts[2] not in _EDIT_PROMPTS:
        await callback.answer("Edición inválida", show_alert=True)
        return
    field = parts[2]
    product_id = int(parts[3])
    await state.set_state(EditProductStates.waiting_value)
    await state.update_data(product_id=product_id, field=field)
    await callback.answer()
    await answer_or_replace(
        callback,
        f"✏️ <b>Editar {h(field)}</b>\n\n{_EDIT_PROMPTS[field]}",
        _admin_cancel_keyboard(),
    )


@router.message(EditProductStates.waiting_value)
async def admin_edit_value(message: Message, state: FSMContext, ctx: AppContext) -> None:
    if not await _require_admin(message, ctx):
        return
    data = await state.get_data()
    product_id = int(data["product_id"])
    field = str(data["field"])
    async with ctx.session_factory() as session:
        product = await session.get(Product, product_id)
        if product is None:
            await state.clear()
            await message.answer("Producto no encontrado.")
            return

        if field == "name":
            value = (message.text or "").strip()
            if not value or len(value) > 180:
                await message.answer("Nombre inválido.")
                return
            product.name = value
        elif field == "price":
            try:
                value_decimal = parse_money(message.text or "")
            except ValueError:
                value_decimal = Decimal("0")
            if value_decimal <= 0:
                await message.answer("Precio inválido.")
                return
            product.price = value_decimal
            if product.is_external:
                product.provider_price_locked = True
        elif field == "emoji":
            try:
                selection = extract_product_icon(message)
                product.button_emoji = await _preview_product_icon(message, selection.value)
            except ValueError as exc:
                await message.answer(f"❌ {h(exc)}")
                return
            if selection.media_type and selection.media_file_id:
                product.media_type = selection.media_type
                product.media_file_id = selection.media_file_id
        elif field == "description":
            value, entities = capture_message_rich_text(message)
            if not value or len(value) > 3000:
                await message.answer("Descripción inválida.")
                return
            product.description = value
            product.description_entities = entities
        elif field == "instructions":
            value, entities = capture_message_rich_text(message)
            if value == "-":
                value = ""
                entities = "[]"
            if len(value) > 3000:
                await message.answer("Las instrucciones no pueden superar 3000 caracteres.")
                return
            product.instructions = value
            product.instructions_entities = entities
        elif field == "duration":
            raw = (message.text or "").strip()
            if raw == "-":
                product.service_days = None
            else:
                try:
                    days = int(raw)
                except ValueError:
                    days = 0
                if days < 1 or days > 3650:
                    await message.answer("La duración debe estar entre 1 y 3650 días.")
                    return
                product.service_days = days
        elif field == "media":
            if message.photo:
                product.media_type = "photo"
                product.media_file_id = message.photo[-1].file_id
            elif message.animation:
                product.media_type = "animation"
                product.media_file_id = message.animation.file_id
            elif message.sticker:
                product.media_type = "sticker"
                product.media_file_id = message.sticker.file_id
            elif (message.text or "").strip() == "-":
                product.media_type = None
                product.media_file_id = None
            else:
                await message.answer("Envía una foto, GIF/animación, sticker o <code>-</code>.")
                return
        await session.commit()

    await state.clear()
    await message.answer("✅ Producto actualizado.")
    await _show_admin_product(message, ctx, product_id)


# ---------------------------- ProdSeller API -------------------------------


_PROVIDER_OPEN_STATUSES = ("processing", "pending", "pending_delivery", "manual_review")


async def _show_prodseller_admin(target: Message | CallbackQuery, ctx: AppContext) -> None:
    async with ctx.session_factory() as session:
        imported = int(
            await session.scalar(
                select(func.count(Product.id)).where(Product.provider_code == "prodseller")
            )
            or 0
        )
        active = int(
            await session.scalar(
                select(func.count(Product.id)).where(
                    Product.provider_code == "prodseller",
                    Product.active.is_(True),
                )
            )
            or 0
        )
        pending = int(
            await session.scalar(
                select(func.count(ProviderPurchase.id)).where(
                    ProviderPurchase.status.in_(_PROVIDER_OPEN_STATUSES)
                )
            )
            or 0
        )
        last_sync = await session.scalar(
            select(func.max(Product.provider_synced_at)).where(
                Product.provider_code == "prodseller"
            )
        )

    configured = ctx.prodseller is not None
    status = "✅ Conectada" if configured else "❌ No configurada"
    sync_text = last_sync.strftime("%Y-%m-%d %H:%M UTC") if last_sync else "nunca"
    transport_warning = ""
    if ctx.config.prodseller_base_url.lower().startswith("http://"):
        transport_warning = (
            "\n\n⚠️ <b>Advertencia:</b> la URL usa HTTP sin cifrado. "
            "La API Key y las claves entregadas pueden viajar expuestas. "
            "Solicita HTTPS al proveedor."
        )

    text = (
        "🔌 <b>ProdSeller API</b>\n\n"
        f"Estado: <b>{status}</b>\n"
        f"URL: <code>{h(ctx.config.prodseller_base_url)}</code>\n"
        f"Margen para productos nuevos: <b>{ctx.config.prodseller_markup_percent:g}%</b>\n"
        f"Productos importados: <b>{imported}</b>\n"
        f"Productos activos: <b>{active}</b>\n"
        f"Pedidos por revisar: <b>{pending}</b>\n"
        f"Última sincronización: <b>{h(sync_text)}</b>\n\n"
        "La API Key se configura con <code>configurar_prodseller.bat</code>. "
        "La sincronización normal conserva los precios que hayas editado manualmente."
        f"{transport_warning}"
    )
    rows = []
    if configured:
        rows.extend(
            [
                [
                    button(
                        "🔎 Probar conexión", callback_data="admin:prodseller:test", style="success"
                    )
                ],
                [
                    button(
                        "🔄 Sincronizar catálogo",
                        callback_data="admin:prodseller:sync_keep",
                        style="primary",
                    )
                ],
                [
                    button(
                        f"⚠️ Pedidos pendientes ({pending})",
                        callback_data="admin:prodseller:purchases",
                        style="danger" if pending else "primary",
                    )
                ],
            ]
        )
    rows.append([button("❌ Volver", callback_data="admin:home", style="danger")])
    await answer_or_replace(target, text, InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data == "admin:prodseller")
async def admin_prodseller_home(callback: CallbackQuery, ctx: AppContext) -> None:
    if not await _require_admin(callback, ctx):
        return
    await callback.answer()
    await _show_prodseller_admin(callback, ctx)


@router.callback_query(F.data == "admin:prodseller:test")
async def admin_prodseller_test(callback: CallbackQuery, ctx: AppContext) -> None:
    if not await _require_admin(callback, ctx):
        return
    if ctx.prodseller is None:
        await callback.answer("Configura la API con configurar_prodseller.bat", show_alert=True)
        return
    await callback.answer("Consultando ProdSeller…")
    try:
        balance, products = await asyncio.gather(
            ctx.prodseller.get_balance(),
            ctx.prodseller.list_products(force_refresh=True),
        )
    except ProdSellerError as exc:
        logger.exception("ProdSeller API diagnostic failed")
        await callback.message.answer(
            "❌ <b>Error conectando con ProdSeller</b>\n\n"
            f"<code>{h(type(exc).__name__)}: {h_truncate(str(exc), 1800)}</code>\n\n"
            "Revisa la URL, la API Key, el estado del servidor y el permiso de HTTP inseguro."
        )
        return

    rate = ctx.prodseller.rate_limit
    rate_text = "No informado por el servidor"
    if rate.limit is not None or rate.remaining is not None:
        rate_text = f"{rate.remaining if rate.remaining is not None else '?'} / {rate.limit if rate.limit is not None else '?'}"
    await callback.message.answer(
        "✅ <b>ProdSeller conectado</b>\n\n"
        f"Balance del proveedor: <b>${money(balance.balance)} USDT</b>\n"
        f"Membresía: <b>{h(balance.membership)}</b>\n"
        f"Usuario API: <b>{h(balance.username or 'sin usuario')}</b>\n"
        f"Productos disponibles en respuesta: <b>{len(products)}</b>\n"
        f"Rate limit restante: <b>{h(rate_text)}</b>"
    )


async def _run_prodseller_sync(
    callback: CallbackQuery,
    ctx: AppContext,
    *,
    update_prices: bool,
) -> None:
    if not await _require_admin(callback, ctx):
        return
    if ctx.prodseller is None:
        await callback.answer("ProdSeller no está configurado", show_alert=True)
        return
    await callback.answer("Sincronizando catálogo…")
    try:
        async with ctx.session_factory() as session:
            result = await sync_prodseller_catalog(
                session,
                ctx.prodseller,
                markup_percent=ctx.config.prodseller_markup_percent,
                update_prices=update_prices,
                force_refresh=True,
            )
    except ProdSellerError as exc:
        logger.exception("ProdSeller catalog sync failed")
        await callback.message.answer(
            "❌ No se pudo sincronizar ProdSeller.\n\n"
            f"<code>{h(type(exc).__name__)}: {h_truncate(str(exc), 1800)}</code>"
        )
        return

    await callback.message.answer(
        "✅ <b>Catálogo ProdSeller sincronizado</b>\n\n"
        f"Recibidos: <b>{result.received}</b>\n"
        f"Nuevos: <b>{result.created}</b>\n"
        f"Actualizados: <b>{result.updated}</b>\n"
        f"No disponibles: <b>{result.unavailable}</b>\n"
        f"Precios recalculados: <b>{result.prices_updated}</b>"
    )
    await _show_prodseller_admin(callback, ctx)


@router.callback_query(F.data == "admin:prodseller:sync_keep")
async def admin_prodseller_sync_keep(callback: CallbackQuery, ctx: AppContext) -> None:
    await _run_prodseller_sync(callback, ctx, update_prices=False)


@router.callback_query(F.data == "admin:prodseller:sync_prices")
async def admin_prodseller_sync_prices(callback: CallbackQuery, ctx: AppContext) -> None:
    await _run_prodseller_sync(callback, ctx, update_prices=True)


@router.callback_query(F.data.startswith("admin:prodseller:refresh_product:"))
async def admin_prodseller_refresh_product(callback: CallbackQuery, ctx: AppContext) -> None:
    if not await _require_admin(callback, ctx):
        return
    if ctx.prodseller is None:
        await callback.answer("ProdSeller no está configurado", show_alert=True)
        return
    try:
        product_id = int(callback.data.rsplit(":", 1)[1])
    except (TypeError, ValueError):
        await callback.answer("Producto inválido", show_alert=True)
        return
    try:
        async with ctx.session_factory() as session:
            product = await session.get(Product, product_id)
            if product is None or not product.is_external:
                await callback.answer("Producto API no encontrado", show_alert=True)
                return
            await refresh_prodseller_product(
                session,
                ctx.prodseller,
                product,
                force_refresh=True,
            )
    except ProdSellerError as exc:
        await callback.answer(f"Error: {shorten(str(exc), 120)}", show_alert=True)
        return
    await callback.answer("Producto actualizado")
    await _show_admin_product(callback, ctx, product_id)


async def _show_provider_purchases(target: Message | CallbackQuery, ctx: AppContext) -> None:
    async with ctx.session_factory() as session:
        rows = (
            await session.execute(
                select(ProviderPurchase, User, Product)
                .join(User, User.id == ProviderPurchase.user_id)
                .join(Product, Product.id == ProviderPurchase.product_id)
                .where(ProviderPurchase.status.in_(_PROVIDER_OPEN_STATUSES))
                .order_by(ProviderPurchase.id.desc())
                .limit(30)
            )
        ).all()
    keyboard_rows = []
    for purchase, _user, product in rows:
        keyboard_rows.append(
            [
                button(
                    f"#{purchase.id} · {shorten(product.name, 20)} · {purchase.status}",
                    callback_data=f"admin:prodseller:purchase:{purchase.id}",
                    style="danger" if purchase.status == "manual_review" else "primary",
                )
            ]
        )
    keyboard_rows.append([button("❌ Volver", callback_data="admin:prodseller", style="danger")])
    text = "⚠️ <b>Pedidos ProdSeller por revisar</b>"
    if not rows:
        text += "\n\nNo hay pedidos pendientes."
    await answer_or_replace(target, text, InlineKeyboardMarkup(inline_keyboard=keyboard_rows))


@router.callback_query(F.data == "admin:prodseller:purchases")
async def admin_prodseller_purchases(callback: CallbackQuery, ctx: AppContext) -> None:
    if not await _require_admin(callback, ctx):
        return
    await callback.answer()
    await _show_provider_purchases(callback, ctx)


async def _show_provider_purchase(
    target: Message | CallbackQuery,
    ctx: AppContext,
    purchase_id: int,
) -> None:
    async with ctx.session_factory() as session:
        row = (
            await session.execute(
                select(ProviderPurchase, User, Product)
                .join(User, User.id == ProviderPurchase.user_id)
                .join(Product, Product.id == ProviderPurchase.product_id)
                .where(ProviderPurchase.id == purchase_id)
            )
        ).first()
    if row is None:
        await answer_or_replace(
            target,
            "Pedido API no encontrado.",
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        button(
                            "❌ Volver", callback_data="admin:prodseller:purchases", style="danger"
                        )
                    ]
                ]
            ),
        )
        return
    purchase, user, product = row
    username = f"@{user.username}" if user.username else user.first_name
    text = (
        f"🔌 <b>Pedido API #{purchase.id}</b>\n\n"
        f"Código interno: <code>{h(purchase.purchase_code)}</code>\n"
        f"Estado: <b>{h(purchase.status)}</b>\n"
        f"Cliente: <b>{h(username)}</b> · <code>{user.telegram_id}</code>\n"
        f"Producto: <b>{h_truncate(product.name, 350)}</b>\n"
        f"ID producto proveedor: <code>{h(purchase.provider_product_id)}</code>\n"
        f"Order ID proveedor: <code>{h(purchase.provider_order_id or 'no disponible')}</code>\n"
        f"Cobro local reservado: <b>${money(purchase.local_price)}</b>\n"
        f"Costo esperado: <b>${money(purchase.expected_provider_cost or 0)}</b>\n"
        f"Costo real: <b>${money(purchase.actual_provider_amount or 0)}</b>\n"
        f"Creado: <b>{purchase.created_at.strftime('%Y-%m-%d %H:%M UTC')}</b>\n"
    )
    if purchase.error_message:
        text += f"\n<b>Error:</b>\n<code>{h_truncate(purchase.error_message, 1800)}</code>"

    buttons = []
    if purchase.provider_order_id and purchase.status not in {"delivered", "refunded"}:
        buttons.append(
            [
                button(
                    "🔄 Consultar estado y entregar",
                    callback_data=f"admin:prodseller:retry:{purchase.id}",
                    style="success",
                )
            ]
        )
    if purchase.status not in {"delivered", "refunded"}:
        buttons.append(
            [
                button(
                    "💸 Reembolsar saldo local",
                    callback_data=f"admin:prodseller:refund_confirm:{purchase.id}",
                    style="danger",
                )
            ]
        )
    buttons.append(
        [button("❌ Volver", callback_data="admin:prodseller:purchases", style="danger")]
    )
    await answer_or_replace(target, text, InlineKeyboardMarkup(inline_keyboard=buttons))


@router.callback_query(F.data.startswith("admin:prodseller:purchase:"))
async def admin_prodseller_purchase_detail(callback: CallbackQuery, ctx: AppContext) -> None:
    if not await _require_admin(callback, ctx):
        return
    try:
        purchase_id = int(callback.data.rsplit(":", 1)[1])
    except (TypeError, ValueError):
        await callback.answer("Pedido inválido", show_alert=True)
        return
    await callback.answer()
    await _show_provider_purchase(callback, ctx, purchase_id)


async def _send_api_delivery_to_user(
    bot: Bot,
    user: User,
    result,
) -> None:
    instructions = str(getattr(result, "instructions", "") or "")
    instructions_entities = str(getattr(result, "instructions_entities", "[]") or "[]")
    instructions_block = ""
    if instructions.strip():
        title = (
            "Instrucciones de activación" if user.language == "es" else "Activation instructions"
        )
        instructions_block = (
            f"📋 <b>{title}</b>\n"
            + render_rich_text(
                instructions,
                instructions_entities,
                max_chars=1800,
            )
            + "\n\n"
        )
    delivery = t(
        user.language,
        "purchase_success",
        order=h(result.order_code),
        name=h_truncate(result.product_name, 300),
        price=money(result.price),
        balance=money(result.new_balance),
        instructions_block=instructions_block,
        payload=h(result.stock_payload),
    )
    delivery = ensure_html_block_before(
        delivery,
        instructions_block,
        markers=("📦 <b>Tu producto:</b>", "📦 <b>Your product:</b>"),
    )
    if len(delivery) <= 4000:
        try:
            await bot.send_message(user.telegram_id, delivery)
        except TelegramBadRequest:
            if "<tg-emoji" not in delivery:
                raise
            await bot.send_message(user.telegram_id, strip_custom_emoji_entities(delivery))
    else:
        summary = t(
            user.language,
            "purchase_success_file",
            order=h(result.order_code),
            name=h_truncate(result.product_name, 300),
            price=money(result.price),
            balance=money(result.new_balance),
            instructions_block=instructions_block,
        )
        summary = ensure_html_block_before(
            summary,
            instructions_block,
            markers=("📎",),
        )
        try:
            await bot.send_message(user.telegram_id, summary)
        except TelegramBadRequest:
            if "<tg-emoji" not in summary:
                raise
            await bot.send_message(user.telegram_id, strip_custom_emoji_entities(summary))
        await bot.send_document(
            user.telegram_id,
            BufferedInputFile(
                result.stock_payload.encode("utf-8"),
                filename=f"{result.order_code}.txt",
            ),
            caption=t(user.language, "product_file_caption", order=h(result.order_code)),
        )
    await bot.send_message(
        user.telegram_id,
        t(user.language, "purchase_continue"),
    )


@router.callback_query(F.data.startswith("admin:prodseller:retry:"))
async def admin_prodseller_retry(
    callback: CallbackQuery,
    bot: Bot,
    ctx: AppContext,
) -> None:
    if not await _require_admin(callback, ctx):
        return
    if ctx.prodseller is None:
        await callback.answer("ProdSeller no está configurado", show_alert=True)
        return
    try:
        purchase_id = int(callback.data.rsplit(":", 1)[1])
    except (TypeError, ValueError):
        await callback.answer("Pedido inválido", show_alert=True)
        return
    await callback.answer("Consultando proveedor…")
    try:
        result = await retry_provider_purchase(
            ctx.session_factory,
            ctx.prodseller,
            purchase_id=purchase_id,
        )
    except (ExternalOrderManualReview, ExternalOrderRejected, ProdSellerError) as exc:
        await callback.message.answer(
            "❌ No se pudo resolver el pedido.\n\n"
            f"<code>{h(type(exc).__name__)}: {h_truncate(str(exc), 1800)}</code>"
        )
        await _show_provider_purchase(callback, ctx, purchase_id)
        return

    async with ctx.session_factory() as session:
        purchase = await session.get(ProviderPurchase, purchase_id)
        user = await session.get(User, purchase.user_id) if purchase else None
    if purchase is None or user is None:
        await callback.message.answer("❌ El pedido local o el usuario ya no existe.")
        return

    if result is not None:
        try:
            await _send_api_delivery_to_user(bot, user, result)
            await callback.message.answer("✅ Pedido entregado al cliente y guardado en Historial.")
        except Exception:
            logger.exception(
                "Could not deliver resolved provider purchase to user %s", user.telegram_id
            )
            await callback.message.answer(
                "⚠️ La orden quedó guardada como entregada, pero Telegram no permitió enviar el mensaje. "
                "El cliente puede recuperarla desde Historial."
            )
    elif purchase.status == "refunded":
        try:
            await bot.send_message(
                user.telegram_id,
                "💸 El pedido del proveedor no se completó y el saldo fue devuelto a tu wallet."
                if user.language == "es"
                else "💸 The provider order was not completed and your balance was refunded.",
            )
        except Exception:
            logger.exception("Could not notify user about provider refund")
        await callback.message.answer("💸 Pedido fallido; saldo local reembolsado.")
    else:
        await callback.message.answer("🕓 El proveedor todavía mantiene la orden pendiente.")
    await _show_provider_purchase(callback, ctx, purchase_id)


@router.callback_query(F.data.startswith("admin:prodseller:refund_confirm:"))
async def admin_prodseller_refund_confirm(callback: CallbackQuery, ctx: AppContext) -> None:
    if not await _require_admin(callback, ctx):
        return
    try:
        purchase_id = int(callback.data.rsplit(":", 1)[1])
    except (TypeError, ValueError):
        await callback.answer("Pedido inválido", show_alert=True)
        return
    await callback.answer()
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                button(
                    "✅ Sí, reembolsar",
                    callback_data=f"admin:prodseller:refund:{purchase_id}",
                    style="danger",
                )
            ],
            [
                button(
                    "❌ No, volver",
                    callback_data=f"admin:prodseller:purchase:{purchase_id}",
                    style="primary",
                )
            ],
        ]
    )
    await answer_or_replace(
        callback,
        "⚠️ <b>Confirma el reembolso</b>\n\n"
        "Hazlo únicamente después de comprobar en el panel de ProdSeller que la orden "
        "no fue cobrada ni entregada. El bot no repetirá automáticamente un POST ambiguo.",
        markup,
    )


@router.callback_query(F.data.startswith("admin:prodseller:refund:"))
async def admin_prodseller_refund(
    callback: CallbackQuery,
    bot: Bot,
    ctx: AppContext,
) -> None:
    if not await _require_admin(callback, ctx):
        return
    try:
        purchase_id = int(callback.data.rsplit(":", 1)[1])
    except (TypeError, ValueError):
        await callback.answer("Pedido inválido", show_alert=True)
        return
    async with ctx.session_factory() as session:
        refunded = await refund_provider_purchase(
            session,
            purchase_id=purchase_id,
            reason="manual_admin_refund",
        )
    async with ctx.session_factory() as session:
        purchase = await session.get(ProviderPurchase, purchase_id)
        user = await session.get(User, purchase.user_id) if purchase else None
    if not refunded:
        await callback.answer("El pedido ya fue entregado o reembolsado", show_alert=True)
        await _show_provider_purchase(callback, ctx, purchase_id)
        return
    await callback.answer("Saldo reembolsado")
    if user is not None:
        try:
            await bot.send_message(
                user.telegram_id,
                "💸 El administrador reembolsó el saldo de tu pedido pendiente de proveedor."
                if user.language == "es"
                else "💸 The administrator refunded the balance for your pending provider order.",
            )
        except Exception:
            logger.exception("Could not notify user about manual provider refund")
    await callback.message.answer("✅ Saldo local reembolsado.")
    await _show_provider_purchase(callback, ctx, purchase_id)


# ---------------------------- Runtime settings -----------------------------


_SETTING_LABELS = {
    STORE_NAME: "Nombre de la tienda",
    BINANCE_PAY_ID: "Binance Pay ID",
    BINANCE_PAY_NAME: "Nombre de Binance",
    SUPPORT_USERNAME: "Usuario de soporte",
    BONUS_TIERS: "Bonos",
}


async def _show_admin_settings(target: Message | CallbackQuery, ctx: AppContext) -> None:
    async with ctx.session_factory() as session:
        profile = await get_store_profile(session)
    api_status = "✅ Configurada" if ctx.binance is not None else "❌ No configurada"
    provider_status = f"✅ {len(ctx.providers)} activa(s)" if len(ctx.providers) else "❌ Ninguna"
    text = (
        "⚙️ <b>Configuración</b>\n\n"
        f"Tienda: <b>{h(profile.name)}</b>\n"
        f"Pay ID: <code>{h(profile.binance_pay_id or 'sin configurar')}</code>\n"
        f"Nombre Binance: <b>{h(profile.binance_pay_name or 'sin configurar')}</b>\n"
        f"Soporte: <b>@{h(profile.support_username)}</b>\n"
        f"Bonos: <code>{h(profile.bonus_tiers_raw)}</code>\n"
        f"API Binance: <b>{api_status}</b>\n"
        f"Proveedores API: <b>{provider_status}</b>\n\n"
        "El token y ADMIN_IDS se cambian en <code>.env</code>. Las APIs se administran con <code>configurar_apis.bat</code>."
    )
    rows = [
        [button("🏪 Nombre tienda", callback_data=f"admin:setting:{STORE_NAME}", style="primary")],
        [button("🆔 Pay ID", callback_data=f"admin:setting:{BINANCE_PAY_ID}", style="primary")],
        [
            button(
                "👤 Nombre Binance",
                callback_data=f"admin:setting:{BINANCE_PAY_NAME}",
                style="primary",
            )
        ],
        [button("💬 Soporte", callback_data=f"admin:setting:{SUPPORT_USERNAME}", style="primary")],
        [button("🎁 Bonos", callback_data=f"admin:setting:{BONUS_TIERS}", style="primary")],
        [
            button(
                "🔎 Probar API Binance",
                callback_data="admin:binance:test",
                style="success",
            )
        ],
        [button("❌ Volver", callback_data="admin:home", style="danger")],
    ]
    await answer_or_replace(target, text, InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data == "admin:settings")
async def admin_settings(callback: CallbackQuery, ctx: AppContext) -> None:
    if not await _require_admin(callback, ctx):
        return
    await callback.answer()
    await _show_admin_settings(callback, ctx)


@router.callback_query(F.data == "admin:binance:test")
async def admin_binance_test(callback: CallbackQuery, ctx: AppContext) -> None:
    if not await _require_admin(callback, ctx):
        return
    if ctx.binance is None:
        await callback.answer(
            "Faltan BINANCE_API_KEY y BINANCE_API_SECRET en .env",
            show_alert=True,
        )
        return
    await callback.answer("Consultando Binance…")
    try:
        diagnostic = await ctx.binance.diagnose(force_refresh=True)
    except Exception as exc:
        logger.exception("Binance API diagnostic failed")
        await callback.message.answer(
            "❌ <b>La API de Binance respondió con error</b>\n\n"
            f"<code>{h(type(exc).__name__)}: {h(exc)}</code>\n\n"
            "Revisa que API Key y Secret pertenezcan a la cuenta receptora, "
            "que la clave tenga lectura habilitada y que la hora de Windows sea automática."
        )
        return

    latest = "ninguno"
    if diagnostic.latest_transaction_time_ms is not None:
        latest = datetime.fromtimestamp(
            diagnostic.latest_transaction_time_ms / 1000,
            tz=UTC,
        ).strftime("%Y-%m-%d %H:%M:%S UTC")
    recent_ids = (
        "\n".join(f"• <code>{h(order_id)}</code>" for order_id in diagnostic.recent_order_ids)
        or "• Ninguno"
    )
    await callback.message.answer(
        "✅ <b>API de Binance conectada</b>\n\n"
        f"Movimientos obtenidos: <b>{diagnostic.transaction_count}</b>\n"
        f"Ingresos positivos en USDT: <b>{diagnostic.incoming_usdt_count}</b>\n"
        f"Movimiento más reciente: <b>{h(latest)}</b>\n\n"
        "<b>Order ID recientes visibles para esta API:</b>\n"
        f"{recent_ids}\n\n"
        "El cliente debe enviar solamente el número. Si el pago aparece en Binance "
        "pero su Order ID no aparece aquí, la API Key pertenece a otra cuenta o "
        "Binance no expone ese movimiento a esa clave."
    )


@router.callback_query(F.data.startswith("admin:setting:"))
async def admin_setting_start(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    if not await _require_admin(callback, ctx):
        return
    key = callback.data.split(":", 2)[2]
    if key not in _SETTING_LABELS:
        await callback.answer("Configuración inválida", show_alert=True)
        return
    await state.set_state(EditSettingStates.waiting_value)
    await state.update_data(setting_key=key)
    hint = ""
    if key == BONUS_TIERS:
        hint = "\nFormato: <code>50:2,100:5</code>"
    elif key == SUPPORT_USERNAME:
        hint = "\nEnvía el usuario sin @. Usa <code>-</code> para vaciar."
    await callback.answer()
    await answer_or_replace(
        callback,
        f"⚙️ <b>{h(_SETTING_LABELS[key])}</b>\n\nEnvía el nuevo valor.{hint}",
        _admin_cancel_keyboard(),
    )


@router.message(EditSettingStates.waiting_value)
async def admin_setting_value(message: Message, state: FSMContext, ctx: AppContext) -> None:
    if not await _require_admin(message, ctx):
        return
    data = await state.get_data()
    key = str(data["setting_key"])
    value = (message.text or "").strip()
    if value == "-":
        value = ""
    if key == STORE_NAME and not value:
        await message.answer("El nombre de la tienda no puede estar vacío.")
        return
    if key == BONUS_TIERS:
        try:
            parse_bonus_tiers(value)
        except ValueError as exc:
            await message.answer(f"❌ {h(exc)}")
            return
    if key == SUPPORT_USERNAME:
        value = value.lstrip("@")
    async with ctx.session_factory() as session:
        await set_runtime_setting(session, key, value)
    await state.clear()
    await message.answer("✅ Configuración actualizada.")
    await _show_admin_settings(message, ctx)


# ------------------------------ Deposits -----------------------------------


@router.callback_query(F.data == "admin:deposits")
async def admin_deposits(callback: CallbackQuery, ctx: AppContext) -> None:
    if not await _require_admin(callback, ctx):
        return
    await callback.answer()
    async with ctx.session_factory() as session:
        deposits = (
            await session.scalars(
                select(Deposit)
                .order_by((Deposit.status == "pending").desc(), Deposit.id.desc())
                .limit(25)
            )
        ).all()
        user_ids = {dep.user_id for dep in deposits}
        users = {}
        if user_ids:
            users = {
                user.id: user
                for user in (await session.scalars(select(User).where(User.id.in_(user_ids)))).all()
            }
    rows = []
    status_icon = {"pending": "🕓", "credited": "✅", "rejected": "❌", "cancelled": "⚪"}
    for dep in deposits:
        user = users.get(dep.user_id)
        label_user = (
            f"@{user.username}"
            if user and user.username
            else str(user.telegram_id if user else dep.user_id)
        )
        rows.append(
            [
                button(
                    f"{status_icon.get(dep.status, '•')} #{dep.id} ${money(dep.requested_amount)} {shorten(label_user, 16)}",
                    callback_data=f"admin:deposit:{dep.id}",
                    style="primary" if dep.status == "pending" else None,
                )
            ]
        )
    rows.append([button("❌ Volver", callback_data="admin:home", style="danger")])
    text = "💳 <b>Depósitos recientes</b>"
    if not deposits:
        text += "\n\nNo hay depósitos."
    await answer_or_replace(callback, text, InlineKeyboardMarkup(inline_keyboard=rows))


async def _show_deposit(target: Message | CallbackQuery, ctx: AppContext, deposit_id: int) -> None:
    async with ctx.session_factory() as session:
        dep = await session.get(Deposit, deposit_id)
        user = await session.get(User, dep.user_id) if dep else None
    if dep is None:
        await answer_or_replace(target, "Depósito no encontrado.", _admin_home_keyboard())
        return
    user_label = (
        f"@{h(user.username)}"
        if user and user.username
        else h(user.telegram_id if user else dep.user_id)
    )
    text = (
        f"💳 <b>Depósito #{dep.id}</b>\n\n"
        f"Usuario: {user_label}\n"
        f"Telegram ID: <code>{user.telegram_id if user else '—'}</code>\n"
        f"Monto: <b>${money(dep.requested_amount)} USDT</b>\n"
        f"Estado: <b>{h(dep.status)}</b>\n"
        f"ID declarado: <code>{h(dep.claimed_transaction_id or '—')}</code>\n"
        f"ID acreditado: <code>{h(dep.transaction_id or '—')}</code>\n"
        f"Intentos: <b>{dep.verify_attempts}</b>\n"
        f"Motivo/error: <code>{h(dep.failure_reason or '—')}</code>"
    )
    rows = []
    if dep.status == "pending":
        rows.append(
            [
                button(
                    "✅ Aprobar manualmente",
                    callback_data=f"admin:deposit:approve:{dep.id}",
                    style="success",
                )
            ]
        )
        rows.append(
            [button("❌ Rechazar", callback_data=f"admin:deposit:reject:{dep.id}", style="danger")]
        )
    rows.append([button("⬅️ Volver", callback_data="admin:deposits", style="danger")])
    await answer_or_replace(target, text, InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("admin:deposit:approve:"))
async def admin_deposit_approve(callback: CallbackQuery, bot: Bot, ctx: AppContext) -> None:
    if not await _require_admin(callback, ctx):
        return
    deposit_id = int(callback.data.rsplit(":", 1)[1])
    async with ctx.session_factory() as session:
        dep = await session.get(Deposit, deposit_id)
        if dep is None:
            await callback.answer("Depósito no encontrado", show_alert=True)
            return
        user = await session.get(User, dep.user_id)
        profile = await get_store_profile(session)
    reference = dep.claimed_transaction_id or f"MANUAL-{dep.id}"
    try:
        async with ctx.session_factory() as session:
            result = await credit_deposit(
                session,
                deposit_id=deposit_id,
                transaction_id=reference,
                raw_payload='{"manual":true}',
                bonus_tiers=profile.bonus_tiers_raw,
            )
    except DuplicateTransaction:
        await callback.answer("Ese ID ya fue acreditado", show_alert=True)
        return
    except DepositAlreadyProcessed:
        await callback.answer("El depósito ya fue procesado", show_alert=True)
        return
    await callback.answer("Depósito acreditado")
    if user:
        try:
            await bot.send_message(
                user.telegram_id,
                "✅ <b>Tu depósito fue aprobado</b>\n\n"
                f"Acreditado: <b>${money(result.total)}</b>\n"
                f"Nuevo balance: <b>${money(result.new_balance)}</b>",
            )
        except Exception:
            logger.exception("Could not notify user about manual approval")
    await _show_deposit(callback, ctx, deposit_id)


@router.callback_query(F.data.startswith("admin:deposit:reject:"))
async def admin_deposit_reject(callback: CallbackQuery, bot: Bot, ctx: AppContext) -> None:
    if not await _require_admin(callback, ctx):
        return
    deposit_id = int(callback.data.rsplit(":", 1)[1])
    async with ctx.session_factory() as session:
        dep = await reject_deposit(
            session, deposit_id=deposit_id, reason="Rejected by administrator"
        )
        user = await session.get(User, dep.user_id) if dep else None
    if dep is None:
        await callback.answer("El depósito ya fue procesado", show_alert=True)
        return
    await callback.answer("Depósito rechazado")
    if user:
        try:
            await bot.send_message(
                user.telegram_id,
                f"❌ Tu solicitud de depósito <code>#{deposit_id}</code> fue rechazada. Contacta a soporte si necesitas revisión.",
            )
        except Exception:
            logger.exception("Could not notify user about rejection")
    await _show_deposit(callback, ctx, deposit_id)


@router.callback_query(F.data.startswith("admin:deposit:"))
async def admin_deposit_detail(callback: CallbackQuery, ctx: AppContext) -> None:
    if not await _require_admin(callback, ctx):
        return
    parts = callback.data.split(":")
    if len(parts) != 3:
        return
    await callback.answer()
    await _show_deposit(callback, ctx, int(parts[2]))
