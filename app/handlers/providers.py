from __future__ import annotations

import asyncio
import logging
import math
from decimal import Decimal

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy import func, select, update

from app.context import AppContext
from app.handlers.helpers import answer_or_replace
from app.keyboards import button
from app.models import Product, ProviderPurchase, User
from app.services.external_purchases import (
    ExternalOrderManualReview,
    ExternalOrderRejected,
    refund_provider_purchase,
    retry_provider_purchase,
)
from app.services.notifications import (
    ProductNotice,
    broadcast_catalog_update,
    broadcast_product_available,
)
from app.services.prodseller import ProdSellerError
from app.services.provider_catalog import (
    notify_provider_sync_changes,
    refresh_provider_product,
    sync_provider_catalog,
)
from app.services.settings import get_provider_auto_publish, set_provider_auto_publish
from app.utils import h, h_truncate, money, shorten

logger = logging.getLogger(__name__)
router = Router(name="providers")

PAGE_SIZE = 7
OPEN_STATUSES = ("processing", "pending", "pending_delivery", "manual_review")


async def _require_admin(event: Message | CallbackQuery, ctx: AppContext) -> bool:
    if event.from_user.id in ctx.config.admin_ids:
        return True
    if isinstance(event, CallbackQuery):
        await event.answer("Acceso denegado", show_alert=True)
    else:
        await event.answer("Acceso denegado")
    return False


def _runtime(ctx: AppContext, code: str):  # type: ignore[no-untyped-def]
    return ctx.providers.get(code)


async def _show_providers_home(target: Message | CallbackQuery, ctx: AppContext) -> None:
    runtimes = ctx.providers.values()
    async with ctx.session_factory() as session:
        product_counts = {
            str(code): int(count)
            for code, count in (
                await session.execute(
                    select(Product.provider_code, func.count(Product.id))
                    .where(Product.provider_code.is_not(None))
                    .group_by(Product.provider_code)
                )
            ).all()
            if code
        }
        active_counts = {
            str(code): int(count)
            for code, count in (
                await session.execute(
                    select(Product.provider_code, func.count(Product.id))
                    .where(Product.provider_code.is_not(None), Product.active.is_(True))
                    .group_by(Product.provider_code)
                )
            ).all()
            if code
        }
        pending_counts = {
            str(code): int(count)
            for code, count in (
                await session.execute(
                    select(ProviderPurchase.provider_code, func.count(ProviderPurchase.id))
                    .where(ProviderPurchase.status.in_(OPEN_STATUSES))
                    .group_by(ProviderPurchase.provider_code)
                )
            ).all()
            if code
        }

    rows = []
    for runtime in runtimes:
        code = runtime.config.code
        imported = product_counts.get(code, 0)
        active = active_counts.get(code, 0)
        pending = pending_counts.get(code, 0)
        rows.append(
            [
                button(
                    f"🔌 {shorten(runtime.config.name, 24)} | {active}/{imported} | ⚠️ {pending}",
                    callback_data=f"apihome:{code}",
                    style="success" if pending == 0 else "danger",
                )
            ]
        )
    rows.append([button("❌ Volver", callback_data="admin:home", style="danger")])

    text = (
        "🔌 <b>Proveedores API</b>\n\n"
        f"Proveedores activos: <b>{len(runtimes)}</b>\n\n"
        "Todos los productos activos, sean locales o externos, aparecen juntos en la misma "
        "lista de la tienda. El origen solo se muestra en el panel administrativo.\n\n"
        "Por defecto, los productos nuevos de una API se importan <b>desactivados</b>. "
        "Puedes seleccionarlos manualmente o activar la publicación automática por proveedor. "
        "La sincronización actualiza costo y stock, pero nunca cambia el precio de venta que "
        "hayas definido.\n\n"
        "Para agregar, editar o eliminar conexiones ejecuta <code>configurar_apis.bat</code> "
        "y reinicia el bot. Esta versión incluye adaptadores para ProdSeller API v1 "
        "y Canboso Buyer API 1.2."
    )
    if not runtimes:
        text += "\n\nNo hay proveedores activos."
    await answer_or_replace(target, text, InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data == "admin:providers")
async def providers_home(callback: CallbackQuery, ctx: AppContext) -> None:
    if not await _require_admin(callback, ctx):
        return
    await callback.answer()
    await _show_providers_home(callback, ctx)


async def _show_provider(target: Message | CallbackQuery, ctx: AppContext, code: str) -> None:
    runtime = _runtime(ctx, code)
    if runtime is None:
        await answer_or_replace(
            target,
            "Proveedor no configurado o desactivado. Ejecuta configurar_apis.bat y reinicia.",
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [button("❌ Volver", callback_data="admin:providers", style="danger")]
                ]
            ),
        )
        return

    async with ctx.session_factory() as session:
        imported = int(
            await session.scalar(
                select(func.count(Product.id)).where(Product.provider_code == code)
            )
            or 0
        )
        active = int(
            await session.scalar(
                select(func.count(Product.id)).where(
                    Product.provider_code == code,
                    Product.active.is_(True),
                )
            )
            or 0
        )
        pending = int(
            await session.scalar(
                select(func.count(ProviderPurchase.id)).where(
                    ProviderPurchase.provider_code == code,
                    ProviderPurchase.status.in_(OPEN_STATUSES),
                )
            )
            or 0
        )
        last_sync = await session.scalar(
            select(func.max(Product.provider_synced_at)).where(Product.provider_code == code)
        )
        auto_publish = await get_provider_auto_publish(session, code, default=False)

    sync_text = last_sync.strftime("%Y-%m-%d %H:%M UTC") if last_sync else "nunca"
    warning = ""
    if runtime.config.base_url.lower().startswith("http://"):
        warning = (
            "\n\n⚠️ <b>HTTP sin cifrado:</b> la API Key y las entregas pueden ser interceptadas."
        )
    text = (
        f"🔌 <b>{h(runtime.config.name)}</b>\n\n"
        f"Código interno: <code>{h(code)}</code>\n"
        f"URL: <code>{h(runtime.config.base_url)}</code>\n"
        f"Productos importados: <b>{imported}</b>\n"
        f"Seleccionados para vender: <b>{active}</b>\n"
        f"Pedidos por revisar: <b>{pending}</b>\n"
        f"Margen inicial para productos nuevos: <b>{runtime.config.markup_percent:g}%</b>\n"
        f"Publicación automática de productos nuevos: "
        f"<b>{'activada' if auto_publish else 'desactivada'}</b>\n"
        f"Última sincronización: <b>{h(sync_text)}</b>\n\n"
        "La sincronización conserva precios, nombres, descripciones, medios y selección manual. "
        "Con la publicación automática desactivada, los productos nuevos solo se notifican a "
        "los administradores hasta que los selecciones."
        f"{warning}"
    )
    rows = [
        [button("🔎 Probar conexión", callback_data=f"apitest:{code}", style="success")],
        [button("🔄 Sincronizar catálogo", callback_data=f"apisync:{code}", style="primary")],
        [
            button(
                "🔔 Auto-publicar nuevos: " + ("Sí" if auto_publish else "No"),
                callback_data=f"apiautopub:{code}",
                style="success" if auto_publish else "primary",
            )
        ],
        [
            button(
                f"✅ Seleccionar productos ({active}/{imported})",
                callback_data=f"apicat:{code}:0",
                style="success",
            )
        ],
        [
            button(
                f"⚠️ Pedidos pendientes ({pending})",
                callback_data=f"apipending:{code}",
                style="danger" if pending else "primary",
            )
        ],
        [button("❌ Volver", callback_data="admin:providers", style="danger")],
    ]
    await answer_or_replace(target, text, InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("apihome:"))
async def provider_detail(callback: CallbackQuery, ctx: AppContext) -> None:
    if not await _require_admin(callback, ctx):
        return
    code = callback.data.split(":", 1)[1]
    await callback.answer()
    await _show_provider(callback, ctx, code)


@router.callback_query(F.data.startswith("apiautopub:"))
async def provider_auto_publish_toggle(callback: CallbackQuery, ctx: AppContext) -> None:
    if not await _require_admin(callback, ctx):
        return
    code = callback.data.split(":", 1)[1]
    if _runtime(ctx, code) is None:
        await callback.answer("Proveedor no configurado", show_alert=True)
        return
    async with ctx.session_factory() as session:
        current = await get_provider_auto_publish(session, code, default=False)
        await set_provider_auto_publish(session, code, not current)
    enabled = not current
    await callback.answer(
        "Publicación automática activada" if enabled else "Publicación automática desactivada"
    )
    await _show_provider(callback, ctx, code)


@router.callback_query(F.data.startswith("apitest:"))
async def provider_test(callback: CallbackQuery, ctx: AppContext) -> None:
    if not await _require_admin(callback, ctx):
        return
    code = callback.data.split(":", 1)[1]
    runtime = _runtime(ctx, code)
    if runtime is None:
        await callback.answer("Proveedor no configurado", show_alert=True)
        return
    await callback.answer("Consultando proveedor…")
    try:
        balance, products = await asyncio.gather(
            runtime.client.get_balance(),
            runtime.client.list_products(force_refresh=True),
        )
    except ProdSellerError as exc:
        logger.exception("Provider diagnostic failed for %s", code)
        await callback.message.answer(
            f"❌ <b>Error conectando con {h(runtime.config.name)}</b>\n\n"
            f"<code>{h(type(exc).__name__)}: {h_truncate(str(exc), 1800)}</code>"
        )
        return

    rate = runtime.client.rate_limit
    rate_text = "No informado"
    if rate.limit is not None or rate.remaining is not None:
        rate_text = (
            f"{rate.remaining if rate.remaining is not None else '?'} / "
            f"{rate.limit if rate.limit is not None else '?'}"
        )
    currency = (balance.currency or "USDT").upper()
    if currency in {"USD", "USDT"}:
        balance_text = f"${money(balance.balance)} {currency}"
    elif balance.balance_text:
        balance_text = h(balance.balance_text)
        if balance.balance > 0:
            balance_text += f" (equivalente API: ${money(balance.balance)} USDT)"
    else:
        balance_text = f"{money(balance.balance)} {h(currency)}"

    await callback.message.answer(
        f"✅ <b>{h(runtime.config.name)} conectado</b>\n\n"
        f"Balance: <b>{balance_text}</b>\n"
        f"Membresía: <b>{h(balance.membership)}</b>\n"
        f"Usuario API: <b>{h(balance.username or 'sin usuario')}</b>\n"
        f"Productos devueltos: <b>{len(products)}</b>\n"
        f"Rate limit restante: <b>{h(rate_text)}</b>"
    )


@router.callback_query(F.data.startswith("apisync:"))
async def provider_sync(callback: CallbackQuery, bot: Bot, ctx: AppContext) -> None:
    if not await _require_admin(callback, ctx):
        return
    code = callback.data.split(":", 1)[1]
    runtime = _runtime(ctx, code)
    if runtime is None:
        await callback.answer("Proveedor no configurado", show_alert=True)
        return
    await callback.answer("Sincronizando catálogo…")
    try:
        async with ctx.session_factory() as session:
            auto_publish = await get_provider_auto_publish(session, code, default=False)
            result = await sync_provider_catalog(
                session,
                runtime.client,
                provider_code=code,
                markup_percent=runtime.config.markup_percent,
                force_refresh=True,
                new_products_active=auto_publish,
            )
    except ProdSellerError as exc:
        logger.exception("Provider catalog sync failed for %s", code)
        await callback.message.answer(
            f"❌ No se pudo sincronizar {h(runtime.config.name)}.\n\n"
            f"<code>{h(type(exc).__name__)}: {h_truncate(str(exc), 1800)}</code>"
        )
        return

    if result.created_products or result.restocked_products:
        ctx.spawn(
            notify_provider_sync_changes(
                bot,
                ctx.session_factory,
                provider_name=runtime.config.name,
                result=result,
                admin_ids=ctx.config.admin_ids,
            )
        )

    new_status = "publicados" if auto_publish else "desactivados"
    await callback.message.answer(
        f"✅ <b>{h(runtime.config.name)} sincronizado</b>\n\n"
        f"Recibidos: <b>{result.received}</b>\n"
        f"Nuevos {new_status}: <b>{result.created}</b>\n"
        f"Stock/costo actualizados: <b>{result.updated}</b>\n"
        f"Disponibles nuevamente: <b>{len(result.restocked_products)}</b>\n"
        f"Ya no disponibles: <b>{result.unavailable}</b>\n"
        "Precios de venta modificados: <b>0</b>"
    )
    await _show_provider(callback, ctx, code)


async def _show_catalog(
    target: Message | CallbackQuery,
    ctx: AppContext,
    code: str,
    page: int,
) -> None:
    runtime = _runtime(ctx, code)
    if runtime is None:
        await _show_providers_home(target, ctx)
        return
    requested_page = max(0, page)
    async with ctx.session_factory() as session:
        total = int(
            await session.scalar(
                select(func.count(Product.id)).where(Product.provider_code == code)
            )
            or 0
        )
        page_count = max(1, math.ceil(total / PAGE_SIZE))
        page = min(requested_page, page_count - 1)
        products = (
            await session.scalars(
                select(Product)
                .where(Product.provider_code == code)
                .order_by(Product.active.desc(), Product.name.asc(), Product.id.asc())
                .offset(page * PAGE_SIZE)
                .limit(PAGE_SIZE)
            )
        ).all()
    rows = []
    for product in products:
        selected = "✅" if product.active else "⚫"
        availability = "🟢" if product.provider_in_stock is not False else "🔴"
        cost = money(product.provider_cost or Decimal("0"))
        rows.append(
            [
                button(
                    f"{selected} {availability} {shorten(product.name, 20)} | ${money(product.price)}",
                    callback_data=f"apiselect:{product.id}:{page}",
                    style="success" if product.active else "primary",
                ),
                button(
                    f"✏️ ${cost}",
                    callback_data=f"admin:product:{product.id}",
                    style="primary",
                ),
            ]
        )

    nav = []
    if page > 0:
        nav.append(button("⬅️", callback_data=f"apicat:{code}:{page - 1}", style="primary"))
    nav.append(button(f"{page + 1}/{page_count}", callback_data="noop"))
    if page + 1 < page_count:
        nav.append(button("➡️", callback_data=f"apicat:{code}:{page + 1}", style="primary"))
    if nav:
        rows.append(nav)
    rows.extend(
        [
            [
                button(
                    "✅ Activar todos con stock",
                    callback_data=f"apiactive:{code}:{page}",
                    style="success",
                )
            ],
            [
                button(
                    "⏸ Desactivar todos",
                    callback_data=f"apiinactive:{code}:{page}",
                    style="danger",
                )
            ],
            [button("❌ Volver", callback_data=f"apihome:{code}", style="danger")],
        ]
    )
    text = (
        f"✅ <b>Selección de productos · {h(runtime.config.name)}</b>\n\n"
        "Pulsa un producto para activarlo o desactivarlo. El botón ✏️ abre su ficha para "
        "cambiar precio, nombre, descripción, emoji o imagen.\n\n"
        "El costo junto a ✏️ es privado y solo se muestra aquí. Los clientes ven una sola "
        "lista sin el nombre de la API."
    )
    if not products:
        text += "\n\nSincroniza el catálogo primero."
    await answer_or_replace(target, text, InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("apicat:"))
async def provider_catalog(callback: CallbackQuery, ctx: AppContext) -> None:
    if not await _require_admin(callback, ctx):
        return
    _, code, page_raw = callback.data.split(":", 2)
    try:
        page = int(page_raw)
    except ValueError:
        page = 0
    await callback.answer()
    await _show_catalog(callback, ctx, code, page)


@router.callback_query(F.data.startswith("apiselect:"))
async def provider_select_product(
    callback: CallbackQuery,
    bot: Bot,
    ctx: AppContext,
) -> None:
    if not await _require_admin(callback, ctx):
        return
    _, product_raw, page_raw = callback.data.split(":", 2)
    try:
        product_id = int(product_raw)
        page = int(page_raw)
    except ValueError:
        await callback.answer("Producto inválido", show_alert=True)
        return

    notice: ProductNotice | None = None
    async with ctx.session_factory() as session:
        product = await session.get(Product, product_id)
        if product is None or not product.is_external:
            await callback.answer("Producto externo no encontrado", show_alert=True)
            return
        code = product.provider_code or ""
        was_active = product.active
        product.active = not product.active
        selected = product.active
        available = product.provider_in_stock is not False and (
            product.provider_stock is None or product.provider_stock > 0
        )
        if selected and not was_active and available:
            notice = ProductNotice(
                product_id=product.id,
                name=product.name,
                price=Decimal(product.price),
                button_emoji=product.button_emoji,
            )
        await session.commit()

    await callback.answer("Producto visible" if selected else "Producto oculto")
    if notice is not None:
        ctx.spawn(
            broadcast_product_available(
                bot,
                ctx.session_factory,
                product_id=notice.product_id,
                product_name=notice.name,
                price=notice.price,
                button_emoji=notice.button_emoji,
            )
        )
    await _show_catalog(callback, ctx, code, page)


@router.callback_query(F.data.startswith("apiactive:"))
async def provider_activate_available(
    callback: CallbackQuery,
    bot: Bot,
    ctx: AppContext,
) -> None:
    if not await _require_admin(callback, ctx):
        return
    _, code, page_raw = callback.data.split(":", 2)
    page = int(page_raw) if page_raw.isdigit() else 0
    async with ctx.session_factory() as session:
        products = (
            await session.scalars(
                select(Product).where(
                    Product.provider_code == code,
                    Product.active.is_(False),
                    Product.provider_in_stock.is_not(False),
                    (Product.provider_stock.is_(None) | (Product.provider_stock > 0)),
                )
            )
        ).all()
        notices = [
            ProductNotice(
                product_id=product.id,
                name=product.name,
                price=Decimal(product.price),
                button_emoji=product.button_emoji,
            )
            for product in products
        ]
        for product in products:
            product.active = True
        await session.commit()

    await callback.answer(f"Activados: {len(notices)}")
    if notices:
        ctx.spawn(
            broadcast_catalog_update(
                bot,
                ctx.session_factory,
                products=notices,
                restocked=False,
            )
        )
    await _show_catalog(callback, ctx, code, page)


@router.callback_query(F.data.startswith("apiinactive:"))
async def provider_deactivate_all(callback: CallbackQuery, ctx: AppContext) -> None:
    if not await _require_admin(callback, ctx):
        return
    _, code, page_raw = callback.data.split(":", 2)
    page = int(page_raw) if page_raw.isdigit() else 0
    async with ctx.session_factory() as session:
        result = await session.execute(
            update(Product).where(Product.provider_code == code).values(active=False)
        )
        await session.commit()
    await callback.answer(f"Desactivados: {result.rowcount or 0}")
    await _show_catalog(callback, ctx, code, page)


@router.callback_query(F.data.startswith("admin:provider:refresh_product:"))
async def provider_refresh_product(
    callback: CallbackQuery,
    bot: Bot,
    ctx: AppContext,
) -> None:
    if not await _require_admin(callback, ctx):
        return
    try:
        product_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Producto inválido", show_alert=True)
        return
    async with ctx.session_factory() as session:
        product = await session.get(Product, product_id)
        if product is None or not product.is_external:
            await callback.answer("Producto externo no encontrado", show_alert=True)
            return
        runtime = _runtime(ctx, product.provider_code or "")
        if runtime is None:
            await callback.answer("Proveedor no configurado", show_alert=True)
            return
        was_available = product.provider_in_stock is not False and (
            product.provider_stock is None or product.provider_stock > 0
        )
        try:
            await refresh_provider_product(
                session,
                runtime.client,
                product,
                provider_code=runtime.config.code,
                force_refresh=True,
            )
        except ProdSellerError as exc:
            await callback.answer("No se pudo actualizar", show_alert=True)
            await callback.message.answer(
                f"❌ <code>{h(type(exc).__name__)}: {h_truncate(str(exc), 1600)}</code>"
            )
            return
        is_available = product.provider_in_stock is not False and (
            product.provider_stock is None or product.provider_stock > 0
        )
        notice = (
            ProductNotice(
                product_id=product.id,
                name=product.name,
                price=Decimal(product.price),
                button_emoji=product.button_emoji,
            )
            if product.active and not was_available and is_available
            else None
        )
    await callback.answer("Stock y costo actualizados")
    if notice is not None:
        ctx.spawn(
            broadcast_product_available(
                bot,
                ctx.session_factory,
                product_id=notice.product_id,
                product_name=notice.name,
                price=notice.price,
                restocked=True,
                button_emoji=notice.button_emoji,
            )
        )
    from app.handlers.admin import _show_admin_product

    await _show_admin_product(callback, ctx, product_id)


async def _show_pending(
    target: Message | CallbackQuery,
    ctx: AppContext,
    code: str,
) -> None:
    runtime = _runtime(ctx, code)
    name = runtime.config.name if runtime is not None else code
    async with ctx.session_factory() as session:
        purchases = (
            await session.scalars(
                select(ProviderPurchase)
                .where(
                    ProviderPurchase.provider_code == code,
                    ProviderPurchase.status.in_(OPEN_STATUSES),
                )
                .order_by(ProviderPurchase.id.desc())
                .limit(30)
            )
        ).all()
    rows = [
        [
            button(
                f"⚠️ #{purchase.id} {shorten(purchase.purchase_code, 16)} · {purchase.status}",
                callback_data=f"apipurchase:{purchase.id}",
                style="danger",
            )
        ]
        for purchase in purchases
    ]
    rows.append([button("❌ Volver", callback_data=f"apihome:{code}", style="danger")])
    text = f"⚠️ <b>Pedidos por revisar · {h(name)}</b>"
    if not purchases:
        text += "\n\nNo hay pedidos pendientes."
    await answer_or_replace(target, text, InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("apipending:"))
async def provider_pending(callback: CallbackQuery, ctx: AppContext) -> None:
    if not await _require_admin(callback, ctx):
        return
    code = callback.data.split(":", 1)[1]
    await callback.answer()
    await _show_pending(callback, ctx, code)


async def _show_purchase(
    target: Message | CallbackQuery,
    ctx: AppContext,
    purchase_id: int,
) -> None:
    async with ctx.session_factory() as session:
        purchase = await session.get(ProviderPurchase, purchase_id)
        user = await session.get(User, purchase.user_id) if purchase else None
        product = await session.get(Product, purchase.product_id) if purchase else None
    if purchase is None:
        await answer_or_replace(
            target,
            "Pedido no encontrado.",
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [button("❌ Volver", callback_data="admin:providers", style="danger")]
                ]
            ),
        )
        return
    runtime = _runtime(ctx, purchase.provider_code)
    provider_name = runtime.config.name if runtime is not None else purchase.provider_code
    user_text = (
        f"@{user.username}" if user and user.username else str(user.telegram_id if user else "—")
    )
    text = (
        f"⚠️ <b>Pedido API #{purchase.id}</b>\n\n"
        f"Proveedor: <b>{h(provider_name)}</b>\n"
        f"Código interno: <code>{h(purchase.purchase_code)}</code>\n"
        f"Estado: <b>{h(purchase.status)}</b>\n"
        f"Cliente: <b>{h(user_text)}</b>\n"
        f"Producto: <b>{h(product.name if product else purchase.product_id)}</b>\n"
        f"ID producto proveedor: <code>{h(purchase.provider_product_id)}</code>\n"
        f"Order ID proveedor: <code>{h(purchase.provider_order_id or 'no disponible')}</code>\n"
        f"Venta local: <b>${money(purchase.local_price)}</b>\n"
        f"Costo esperado: <b>${money(purchase.expected_provider_cost or 0)}</b>\n"
        f"Costo real: <b>${money(purchase.actual_provider_amount or 0)}</b>\n"
        f"Parámetros enviados: <code>{h_truncate(purchase.request_payload or '—', 1000)}</code>\n"
        f"Error: <code>{h_truncate(purchase.error_message or '—', 1800)}</code>"
    )
    rows = []
    supports_order_status = bool(runtime and runtime.client.supports_order_status)
    if (
        supports_order_status
        and purchase.provider_order_id
        and purchase.status not in {"delivered", "refunded"}
    ):
        rows.append(
            [
                button(
                    "🔄 Consultar estado",
                    callback_data=f"apiretry:{purchase.id}",
                    style="success",
                )
            ]
        )
    if purchase.status not in {"delivered", "refunded"}:
        rows.append(
            [
                button(
                    "💸 Reembolsar saldo",
                    callback_data=f"apirefundconfirm:{purchase.id}",
                    style="danger",
                )
            ]
        )
    rows.append(
        [
            button(
                "❌ Volver",
                callback_data=f"apipending:{purchase.provider_code}",
                style="danger",
            )
        ]
    )
    await answer_or_replace(target, text, InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("apipurchase:"))
async def provider_purchase_detail(callback: CallbackQuery, ctx: AppContext) -> None:
    if not await _require_admin(callback, ctx):
        return
    try:
        purchase_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Pedido inválido", show_alert=True)
        return
    await callback.answer()
    await _show_purchase(callback, ctx, purchase_id)


@router.callback_query(F.data.startswith("apiretry:"))
async def provider_retry(callback: CallbackQuery, bot: Bot, ctx: AppContext) -> None:
    if not await _require_admin(callback, ctx):
        return
    try:
        purchase_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Pedido inválido", show_alert=True)
        return
    async with ctx.session_factory() as session:
        purchase = await session.get(ProviderPurchase, purchase_id)
    if purchase is None:
        await callback.answer("Pedido no encontrado", show_alert=True)
        return
    runtime = _runtime(ctx, purchase.provider_code)
    if runtime is None:
        await callback.answer("Proveedor no configurado", show_alert=True)
        return
    if not runtime.client.supports_order_status:
        await callback.answer(
            "Este proveedor no ofrece consulta de estado. Revísalo en su panel antes de reembolsar.",
            show_alert=True,
        )
        return
    await callback.answer("Consultando proveedor…")
    try:
        result = await retry_provider_purchase(
            ctx.session_factory,
            runtime.client,
            purchase_id=purchase_id,
        )
    except (ExternalOrderManualReview, ExternalOrderRejected, ProdSellerError) as exc:
        await callback.message.answer(
            "❌ No se pudo resolver el pedido.\n\n"
            f"<code>{h(type(exc).__name__)}: {h_truncate(str(exc), 1800)}</code>"
        )
        await _show_purchase(callback, ctx, purchase_id)
        return

    async with ctx.session_factory() as session:
        purchase = await session.get(ProviderPurchase, purchase_id)
        user = await session.get(User, purchase.user_id) if purchase else None
    if purchase is None or user is None:
        await callback.message.answer("❌ El pedido local o el usuario ya no existe.")
        return
    if result is not None:
        from app.handlers.admin import _send_api_delivery_to_user

        try:
            await _send_api_delivery_to_user(bot, user, result)
            await callback.message.answer("✅ Entregado al cliente y guardado en Historial.")
        except Exception:
            logger.exception("Could not deliver resolved provider order")
            await callback.message.answer(
                "⚠️ Quedó guardado como entregado. El cliente puede recuperarlo desde Historial."
            )
    elif purchase.status == "refunded":
        try:
            await bot.send_message(
                user.telegram_id,
                "💸 El pedido no se completó y el saldo fue devuelto a tu wallet.",
            )
        except Exception:
            logger.exception("Could not notify provider refund")
        await callback.message.answer("💸 Pedido fallido; saldo reembolsado.")
    else:
        await callback.message.answer("🕓 El proveedor todavía mantiene la orden pendiente.")
    await _show_purchase(callback, ctx, purchase_id)


@router.callback_query(F.data.startswith("apirefundconfirm:"))
async def provider_refund_confirm(callback: CallbackQuery, ctx: AppContext) -> None:
    if not await _require_admin(callback, ctx):
        return
    try:
        purchase_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Pedido inválido", show_alert=True)
        return
    await callback.answer()
    await answer_or_replace(
        callback,
        "⚠️ <b>Confirma el reembolso</b>\n\n"
        "Hazlo únicamente después de confirmar en el panel del proveedor que la orden no fue "
        "cobrada ni entregada.",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    button(
                        "✅ Sí, reembolsar",
                        callback_data=f"apirefund:{purchase_id}",
                        style="danger",
                    )
                ],
                [
                    button(
                        "❌ No, volver",
                        callback_data=f"apipurchase:{purchase_id}",
                        style="primary",
                    )
                ],
            ]
        ),
    )


@router.callback_query(F.data.startswith("apirefund:"))
async def provider_refund(callback: CallbackQuery, bot: Bot, ctx: AppContext) -> None:
    if not await _require_admin(callback, ctx):
        return
    try:
        purchase_id = int(callback.data.split(":", 1)[1])
    except ValueError:
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
        await callback.answer("Ya fue entregado o reembolsado", show_alert=True)
        await _show_purchase(callback, ctx, purchase_id)
        return
    await callback.answer("Saldo reembolsado")
    if user is not None:
        try:
            await bot.send_message(
                user.telegram_id,
                "💸 El administrador reembolsó el saldo de tu pedido pendiente.",
            )
        except Exception:
            logger.exception("Could not notify manual provider refund")
    await _show_purchase(callback, ctx, purchase_id)
