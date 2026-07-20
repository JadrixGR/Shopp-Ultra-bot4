from __future__ import annotations

import logging
from decimal import Decimal

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy import func, select

from app.context import AppContext
from app.handlers.helpers import answer_or_replace
from app.keyboards import button
from app.models import BalanceAdjustment, Broadcast, Order, Product, Refund, User
from app.services.admin_finance import (
    BalanceWouldBeNegative,
    FinanceError,
    FinanceOrderNotFound,
    FinanceUserNotFound,
    InvalidRefund,
    NothingToRefund,
    adjust_user_balance,
    build_refund_preview,
    refund_order,
)
from app.services.broadcasts import complete_broadcast, create_broadcast, fail_broadcast
from app.services.notifications import broadcast_announcement
from app.states import AnnouncementStates, BalanceAdjustmentStates, RefundStates
from app.ui_customization import strip_custom_emoji_entities
from app.utils import h, h_truncate, money, parse_money, shorten

logger = logging.getLogger(__name__)
router = Router(name="management")


async def _require_admin(event: Message | CallbackQuery, ctx: AppContext) -> bool:
    if event.from_user.id in ctx.config.admin_ids:
        return True
    if isinstance(event, CallbackQuery):
        await event.answer("Acceso denegado", show_alert=True)
    else:
        await event.answer("Acceso denegado")
    return False


def _cancel_markup(callback_data: str = "admin:home") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[button("❌ Cancelar", callback_data=callback_data, style="danger")]]
    )


def _signed_money(value: Decimal) -> str:
    return f"+${money(value)}" if value > 0 else f"-${money(abs(value))}"


# ------------------------------- Announcements -------------------------------


async def _show_announcements_home(
    target: Message | CallbackQuery,
    ctx: AppContext,
) -> None:
    async with ctx.session_factory() as session:
        total = int(await session.scalar(select(func.count(Broadcast.id))) or 0)
        last = await session.scalar(select(Broadcast).order_by(Broadcast.id.desc()).limit(1))
    last_text = "ninguno"
    if last is not None:
        last_text = f"{last.broadcast_code} · {last.status} · {last.sent}/{last.attempted} enviados"
    text = (
        "📣 <b>Anuncios a usuarios</b>\n\n"
        "Envía un mensaje a todos los usuarios registrados que no estén bloqueados. "
        "El anuncio incluye un botón para abrir la tienda.\n\n"
        f"Anuncios registrados: <b>{total}</b>\n"
        f"Último: <code>{h(last_text)}</code>"
    )
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [button("➕ Crear anuncio", callback_data="admin:announcement:new", style="success")],
            [button("❌ Volver", callback_data="admin:home", style="danger")],
        ]
    )
    await answer_or_replace(target, text, markup)


@router.callback_query(F.data == "admin:announcements")
async def announcements_home(
    callback: CallbackQuery,
    state: FSMContext,
    ctx: AppContext,
) -> None:
    if not await _require_admin(callback, ctx):
        return
    await callback.answer()
    await state.clear()
    await _show_announcements_home(callback, ctx)


@router.callback_query(F.data == "admin:announcement:new")
async def announcement_start(
    callback: CallbackQuery,
    state: FSMContext,
    ctx: AppContext,
) -> None:
    if not await _require_admin(callback, ctx):
        return
    await callback.answer()
    await state.clear()
    await state.set_state(AnnouncementStates.waiting_content)
    await answer_or_replace(
        callback,
        "📣 <b>Nuevo anuncio</b>\n\n"
        "Envía el texto que recibirán los usuarios. Máximo: <b>3500 caracteres</b>.",
        _cancel_markup("admin:announcements"),
    )


@router.message(AnnouncementStates.waiting_content)
async def announcement_content(
    message: Message,
    state: FSMContext,
    ctx: AppContext,
) -> None:
    if not await _require_admin(message, ctx):
        return
    content = (message.html_text or message.text or "").strip()
    if not content or len(content) > 3500:
        await message.answer("Texto inválido. Debe tener entre 1 y 3500 caracteres.")
        return
    await state.update_data(content=content)
    await state.set_state(AnnouncementStates.waiting_confirmation)
    preview = f"📣 <b>Anuncio de la tienda</b>\n\n{content}"
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                button(
                    "✅ Enviar a todos",
                    callback_data="admin:announcement:confirm",
                    style="success",
                )
            ],
            [button("❌ Cancelar", callback_data="admin:announcements", style="danger")],
        ]
    )
    try:
        await message.answer(f"<b>Vista previa</b>\n\n{preview}", reply_markup=markup)
    except TelegramBadRequest:
        await message.answer(
            "<b>Vista previa</b>\n\n" + strip_custom_emoji_entities(preview),
            reply_markup=markup,
        )


async def _run_announcement(
    *,
    bot: Bot,
    ctx: AppContext,
    admin_chat_id: int,
    broadcast_id: int,
    broadcast_code: str,
    content: str,
) -> None:
    try:
        result = await broadcast_announcement(
            bot,
            ctx.session_factory,
            announcement_html=content,
        )
        await complete_broadcast(
            ctx.session_factory,
            broadcast_id=broadcast_id,
            result=result,
        )
        await bot.send_message(
            admin_chat_id,
            "✅ <b>Anuncio terminado</b>\n\n"
            f"Código: <code>{h(broadcast_code)}</code>\n"
            f"Usuarios revisados: <b>{result.attempted}</b>\n"
            f"Enviados: <b>{result.sent}</b>\n"
            f"Bloqueados/no disponibles: <b>{result.blocked}</b>\n"
            f"Fallidos: <b>{result.failed}</b>",
        )
    except Exception:
        logger.exception("Announcement broadcast failed")
        await fail_broadcast(ctx.session_factory, broadcast_id=broadcast_id)
        try:
            await bot.send_message(
                admin_chat_id,
                f"❌ El anuncio <code>{h(broadcast_code)}</code> no pudo completarse.",
            )
        except Exception:
            logger.exception("Could not report announcement failure")


@router.callback_query(F.data == "admin:announcement:confirm")
async def announcement_confirm(
    callback: CallbackQuery,
    state: FSMContext,
    bot: Bot,
    ctx: AppContext,
) -> None:
    if not await _require_admin(callback, ctx):
        return
    if await state.get_state() != AnnouncementStates.waiting_confirmation.state:
        await callback.answer("El anuncio ya no está pendiente", show_alert=True)
        return
    data = await state.get_data()
    content = str(data.get("content") or "").strip()
    if not content:
        await callback.answer("Falta el contenido", show_alert=True)
        return
    async with ctx.session_factory() as session:
        record = await create_broadcast(
            session,
            admin_telegram_id=callback.from_user.id,
            kind="announcement",
            text=content,
        )
    await state.clear()
    await callback.answer("Envío iniciado")
    if callback.message is not None:
        await callback.message.answer(
            "📣 El anuncio se está enviando en segundo plano. "
            f"Código: <code>{h(record.code)}</code>."
        )
    ctx.spawn(
        _run_announcement(
            bot=bot,
            ctx=ctx,
            admin_chat_id=callback.from_user.id,
            broadcast_id=record.id,
            broadcast_code=record.code,
            content=content,
        )
    )


# -------------------------- Refunds and balance tools -------------------------


async def _show_finance_home(target: Message | CallbackQuery, ctx: AppContext) -> None:
    async with ctx.session_factory() as session:
        refund_count = int(await session.scalar(select(func.count(Refund.id))) or 0)
        refund_total = Decimal(
            await session.scalar(select(func.coalesce(func.sum(Refund.amount), 0))) or 0
        )
        adjustment_count = int(await session.scalar(select(func.count(BalanceAdjustment.id))) or 0)
    text = (
        "💸 <b>Reembolsos y saldos</b>\n\n"
        "El prorrateo calcula automáticamente el valor de los días no utilizados. "
        "Los reembolsos y ajustes quedan registrados en la base de datos.\n\n"
        f"Reembolsos: <b>{refund_count}</b> · <b>${money(refund_total)}</b>\n"
        f"Ajustes de saldo registrados: <b>{adjustment_count}</b>"
    )
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                button(
                    "🧾 Reembolsar una compra",
                    callback_data="admin:refund:start",
                    style="success",
                )
            ],
            [
                button(
                    "💵 Dar o ajustar saldo",
                    callback_data="admin:balance:start",
                    style="primary",
                )
            ],
            [button("❌ Volver", callback_data="admin:home", style="danger")],
        ]
    )
    await answer_or_replace(target, text, markup)


@router.callback_query(F.data == "admin:finance")
async def finance_home(
    callback: CallbackQuery,
    state: FSMContext,
    ctx: AppContext,
) -> None:
    if not await _require_admin(callback, ctx):
        return
    await callback.answer()
    await state.clear()
    await _show_finance_home(callback, ctx)


@router.callback_query(F.data == "admin:refund:start")
async def refund_start(
    callback: CallbackQuery,
    state: FSMContext,
    ctx: AppContext,
) -> None:
    if not await _require_admin(callback, ctx):
        return
    await callback.answer()
    await state.clear()
    await state.set_state(RefundStates.waiting_search)
    await answer_or_replace(
        callback,
        "🧾 <b>Buscar compra</b>\n\n"
        "Envía el <b>ID de Telegram del cliente</b> para ver sus últimas compras, "
        "o envía directamente el código de orden, por ejemplo <code>ORD-12AB34CD</code>.",
        _cancel_markup("admin:finance"),
    )


async def _show_user_orders(message: Message, ctx: AppContext, telegram_id: int) -> bool:
    async with ctx.session_factory() as session:
        user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
        if user is None:
            return False
        orders = (
            await session.scalars(
                select(Order).where(Order.user_id == user.id).order_by(Order.id.desc()).limit(15)
            )
        ).all()
    rows = [
        [
            button(
                f"{shorten(order.order_code, 16)} · {shorten(order.product_name, 20)} · ${money(order.price)}",
                callback_data=f"admin:refund:order:{order.id}",
                style="primary",
            )
        ]
        for order in orders
    ]
    rows.append([button("❌ Volver", callback_data="admin:finance", style="danger")])
    text = (
        f"👤 <b>Cliente {telegram_id}</b>\n"
        f"Saldo actual: <b>${money(user.balance)}</b>\n\n"
        "Selecciona la compra que deseas revisar."
    )
    if not orders:
        text += "\n\nEste usuario todavía no tiene compras."
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    return True


@router.message(RefundStates.waiting_search)
async def refund_search(
    message: Message,
    state: FSMContext,
    ctx: AppContext,
) -> None:
    if not await _require_admin(message, ctx):
        return
    query = (message.text or "").strip()
    if not query:
        await message.answer("Envía un ID de Telegram o un código de orden.")
        return

    if query.isdigit():
        if await _show_user_orders(message, ctx, int(query)):
            await state.clear()
            return

    async with ctx.session_factory() as session:
        order = await session.scalar(
            select(Order).where(func.upper(Order.order_code) == query.upper())
        )
    if order is None:
        await message.answer("No se encontró ese usuario ni ese código de orden.")
        return
    await state.clear()
    await _show_refund_order(message, ctx, order.id)


async def _show_refund_order(
    target: Message | CallbackQuery,
    ctx: AppContext,
    order_id: int,
) -> None:
    async with ctx.session_factory() as session:
        row = (
            await session.execute(
                select(Order, User, Product)
                .join(User, User.id == Order.user_id)
                .join(Product, Product.id == Order.product_id)
                .where(Order.id == order_id)
            )
        ).first()
        refund_count = int(
            await session.scalar(select(func.count(Refund.id)).where(Refund.order_id == order_id))
            or 0
        )
    if row is None:
        await answer_or_replace(target, "Compra no encontrada.", _cancel_markup("admin:finance"))
        return
    order, user, product = row
    refunded = Decimal(order.refunded_amount or 0)
    remaining = max(Decimal("0.00"), Decimal(order.price) - refunded)
    duration = f"{product.service_days} días" if product.service_days else "no configurada"
    text = (
        "🧾 <b>Compra para reembolso</b>\n\n"
        f"Orden: <code>{h(order.order_code)}</code>\n"
        f"Cliente: <code>{user.telegram_id}</code>\n"
        f"Producto: <b>{h_truncate(order.product_name, 500)}</b>\n"
        f"Precio pagado: <b>${money(order.price)}</b>\n"
        f"Ya reembolsado: <b>${money(refunded)}</b>\n"
        f"Máximo pendiente: <b>${money(remaining)}</b>\n"
        f"Duración configurada: <b>{h(duration)}</b>\n"
        f"Reembolsos previos: <b>{refund_count}</b>"
    )
    rows = []
    if remaining > 0:
        rows.extend(
            [
                [
                    button(
                        "📆 Calcular prorrateo",
                        callback_data=f"admin:refund:prorate:{order.id}",
                        style="success",
                    )
                ],
                [
                    button(
                        "💯 Reembolso total pendiente",
                        callback_data=f"admin:refund:full:{order.id}",
                        style="primary",
                    )
                ],
            ]
        )
    rows.append([button("❌ Volver", callback_data="admin:finance", style="danger")])
    await answer_or_replace(target, text, InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("admin:refund:order:"))
async def refund_order_detail(
    callback: CallbackQuery,
    state: FSMContext,
    ctx: AppContext,
) -> None:
    if not await _require_admin(callback, ctx):
        return
    try:
        order_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Compra inválida", show_alert=True)
        return
    await callback.answer()
    await state.clear()
    await _show_refund_order(callback, ctx, order_id)


@router.callback_query(F.data.startswith("admin:refund:prorate:"))
async def refund_prorate_start(
    callback: CallbackQuery,
    state: FSMContext,
    ctx: AppContext,
) -> None:
    if not await _require_admin(callback, ctx):
        return
    try:
        order_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Compra inválida", show_alert=True)
        return
    async with ctx.session_factory() as session:
        row = (
            await session.execute(
                select(Order, Product)
                .join(Product, Product.id == Order.product_id)
                .where(Order.id == order_id)
            )
        ).first()
    if row is None:
        await callback.answer("Compra no encontrada", show_alert=True)
        return
    order, product = row
    await state.clear()
    await state.update_data(order_id=order.id, refund_type="prorated")
    await callback.answer()
    if product.service_days:
        await state.update_data(total_days=product.service_days)
        await state.set_state(RefundStates.waiting_used_days)
        await answer_or_replace(
            callback,
            f"📆 Duración configurada: <b>{product.service_days} días</b>.\n\n"
            "Envía cuántos días utilizó el cliente.",
            _cancel_markup("admin:finance"),
        )
        return
    await state.set_state(RefundStates.waiting_total_days)
    await answer_or_replace(
        callback,
        "📆 Envía la duración total del producto en días.\n"
        "Ejemplo: <code>30</code> para un producto de un mes.",
        _cancel_markup("admin:finance"),
    )


@router.message(RefundStates.waiting_total_days)
async def refund_total_days(
    message: Message,
    state: FSMContext,
    ctx: AppContext,
) -> None:
    if not await _require_admin(message, ctx):
        return
    try:
        total_days = int((message.text or "").strip())
    except ValueError:
        total_days = 0
    if total_days < 1 or total_days > 3650:
        await message.answer("La duración debe estar entre 1 y 3650 días.")
        return
    await state.update_data(total_days=total_days)
    await state.set_state(RefundStates.waiting_used_days)
    await message.answer(
        f"Duración total: <b>{total_days} días</b>.\n\n"
        "Ahora envía cuántos días utilizó el cliente.",
        reply_markup=_cancel_markup("admin:finance"),
    )


@router.message(RefundStates.waiting_used_days)
async def refund_used_days(
    message: Message,
    state: FSMContext,
    ctx: AppContext,
) -> None:
    if not await _require_admin(message, ctx):
        return
    data = await state.get_data()
    order_id = int(data.get("order_id") or 0)
    total_days = int(data.get("total_days") or 0)
    try:
        used_days = int((message.text or "").strip())
    except ValueError:
        used_days = -1
    async with ctx.session_factory() as session:
        order = await session.get(Order, order_id)
    if order is None:
        await state.clear()
        await message.answer("Compra no encontrada.")
        return
    try:
        preview = build_refund_preview(
            price=Decimal(order.price),
            already_refunded=Decimal(order.refunded_amount or 0),
            refund_type="prorated",
            total_days=total_days,
            used_days=used_days,
        )
    except FinanceError as exc:
        await message.answer(f"❌ {h(exc)}")
        return

    await state.update_data(used_days=used_days, amount=str(preview.amount_to_credit))
    await state.set_state(RefundStates.waiting_confirmation)
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                button(
                    "✅ Confirmar reembolso",
                    callback_data="admin:refund:confirm",
                    style="success",
                )
            ],
            [button("❌ Cancelar", callback_data="admin:finance", style="danger")],
        ]
    )
    await message.answer(
        "📆 <b>Prorrateo calculado</b>\n\n"
        f"Precio original: <b>${money(preview.original_price)}</b>\n"
        f"Duración: <b>{preview.total_days} días</b>\n"
        f"Días usados: <b>{preview.used_days}</b>\n"
        f"Días no usados: <b>{preview.remaining_days}</b>\n"
        f"Reembolsado previamente: <b>${money(preview.already_refunded)}</b>\n"
        f"Monto a acreditar ahora: <b>${money(preview.amount_to_credit)}</b>",
        reply_markup=markup,
    )


@router.callback_query(F.data.startswith("admin:refund:full:"))
async def refund_full_preview(
    callback: CallbackQuery,
    state: FSMContext,
    ctx: AppContext,
) -> None:
    if not await _require_admin(callback, ctx):
        return
    try:
        order_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Compra inválida", show_alert=True)
        return
    async with ctx.session_factory() as session:
        order = await session.get(Order, order_id)
    if order is None:
        await callback.answer("Compra no encontrada", show_alert=True)
        return
    try:
        preview = build_refund_preview(
            price=Decimal(order.price),
            already_refunded=Decimal(order.refunded_amount or 0),
            refund_type="full",
        )
    except FinanceError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await state.clear()
    await state.update_data(order_id=order_id, refund_type="full")
    await state.set_state(RefundStates.waiting_confirmation)
    await callback.answer()
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                button(
                    "✅ Confirmar reembolso total",
                    callback_data="admin:refund:confirm",
                    style="success",
                )
            ],
            [button("❌ Cancelar", callback_data="admin:finance", style="danger")],
        ]
    )
    await answer_or_replace(
        callback,
        "💯 <b>Reembolso total pendiente</b>\n\n"
        f"Precio original: <b>${money(preview.original_price)}</b>\n"
        f"Ya reembolsado: <b>${money(preview.already_refunded)}</b>\n"
        f"Monto a acreditar ahora: <b>${money(preview.amount_to_credit)}</b>",
        markup,
    )


@router.callback_query(F.data == "admin:refund:confirm")
async def refund_confirm(
    callback: CallbackQuery,
    state: FSMContext,
    bot: Bot,
    ctx: AppContext,
) -> None:
    if not await _require_admin(callback, ctx):
        return
    if await state.get_state() != RefundStates.waiting_confirmation.state:
        await callback.answer("No hay un reembolso pendiente", show_alert=True)
        return
    data = await state.get_data()
    order_id = int(data.get("order_id") or 0)
    refund_type = str(data.get("refund_type") or "")
    total_days = int(data["total_days"]) if data.get("total_days") is not None else None
    used_days = int(data["used_days"]) if data.get("used_days") is not None else None
    reason = (
        f"Prorrateo administrativo: {used_days}/{total_days} días usados"
        if refund_type == "prorated"
        else "Reembolso total administrativo"
    )
    try:
        async with ctx.session_factory() as session:
            result = await refund_order(
                session,
                order_id=order_id,
                admin_telegram_id=callback.from_user.id,
                refund_type=refund_type,
                total_days=total_days,
                used_days=used_days,
                reason=reason,
            )
    except (FinanceOrderNotFound, FinanceUserNotFound, InvalidRefund, NothingToRefund) as exc:
        await callback.answer(str(exc), show_alert=True)
        return

    await state.clear()
    await callback.answer("Reembolso aplicado")
    try:
        await bot.send_message(
            result.telegram_id,
            "💸 <b>Reembolso acreditado</b>\n\n"
            f"Orden: <code>{h(result.order_code)}</code>\n"
            f"Producto: <b>{h_truncate(result.product_name, 500)}</b>\n"
            f"Monto acreditado: <b>${money(result.amount)} USDT</b>\n"
            f"Nuevo saldo: <b>${money(result.new_balance)} USDT</b>",
        )
    except Exception:
        logger.exception("Could not notify refunded user %s", result.telegram_id)
    await answer_or_replace(
        callback,
        "✅ <b>Reembolso registrado</b>\n\n"
        f"Código: <code>{h(result.refund_code)}</code>\n"
        f"Cliente: <code>{result.telegram_id}</code>\n"
        f"Monto: <b>${money(result.amount)}</b>\n"
        f"Total reembolsado de la compra: <b>${money(result.total_refunded)}</b>\n"
        f"Nuevo saldo: <b>${money(result.new_balance)}</b>",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [button("⬅️ Reembolsos y saldos", callback_data="admin:finance", style="primary")]
            ]
        ),
    )


@router.callback_query(F.data == "admin:balance:start")
async def balance_start(
    callback: CallbackQuery,
    state: FSMContext,
    ctx: AppContext,
) -> None:
    if not await _require_admin(callback, ctx):
        return
    await callback.answer()
    await state.clear()
    await state.set_state(BalanceAdjustmentStates.waiting_user_id)
    await answer_or_replace(
        callback,
        "💵 <b>Dar o ajustar saldo</b>\n\nEnvía el ID numérico de Telegram del cliente.",
        _cancel_markup("admin:finance"),
    )


@router.message(BalanceAdjustmentStates.waiting_user_id)
async def balance_user_id(
    message: Message,
    state: FSMContext,
    ctx: AppContext,
) -> None:
    if not await _require_admin(message, ctx):
        return
    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer("El ID de Telegram debe contener únicamente números.")
        return
    telegram_id = int(raw)
    async with ctx.session_factory() as session:
        user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
    if user is None:
        await message.answer("Ese usuario todavía no está registrado en el bot.")
        return
    await state.update_data(telegram_id=telegram_id, current_balance=str(user.balance))
    await state.set_state(BalanceAdjustmentStates.waiting_amount)
    await message.answer(
        f"Cliente: <code>{telegram_id}</code>\n"
        f"Saldo actual: <b>${money(user.balance)}</b>\n\n"
        "Envía el monto. Usa un número positivo para acreditar o negativo para descontar.\n"
        "Ejemplos: <code>5.00</code> o <code>-2.50</code>.",
        reply_markup=_cancel_markup("admin:finance"),
    )


@router.message(BalanceAdjustmentStates.waiting_amount)
async def balance_amount(
    message: Message,
    state: FSMContext,
    ctx: AppContext,
) -> None:
    if not await _require_admin(message, ctx):
        return
    try:
        amount = parse_money(message.text or "")
    except ValueError:
        amount = Decimal("0")
    if amount == 0 or abs(amount) > Decimal("1000000"):
        await message.answer("Monto inválido o fuera del límite permitido.")
        return
    data = await state.get_data()
    current = Decimal(str(data.get("current_balance") or "0"))
    if current + amount < 0:
        await message.answer("El ajuste dejaría el saldo negativo.")
        return
    await state.update_data(amount=str(amount))
    await state.set_state(BalanceAdjustmentStates.waiting_reason)
    await message.answer(
        "Envía el motivo del ajuste. Envía <code>-</code> para usar <i>Ajuste administrativo</i>.",
        reply_markup=_cancel_markup("admin:finance"),
    )


@router.message(BalanceAdjustmentStates.waiting_reason)
async def balance_reason(
    message: Message,
    state: FSMContext,
    ctx: AppContext,
) -> None:
    if not await _require_admin(message, ctx):
        return
    reason = (message.text or "").strip()
    if reason == "-":
        reason = "Ajuste administrativo"
    if not reason or len(reason) > 2000:
        await message.answer("Motivo inválido. Máximo 2000 caracteres.")
        return
    data = await state.get_data()
    telegram_id = int(data["telegram_id"])
    amount = Decimal(str(data["amount"]))
    current = Decimal(str(data["current_balance"]))
    new_balance = current + amount
    await state.update_data(reason=reason)
    await state.set_state(BalanceAdjustmentStates.waiting_confirmation)
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                button(
                    "✅ Aplicar ajuste",
                    callback_data="admin:balance:confirm",
                    style="success",
                )
            ],
            [button("❌ Cancelar", callback_data="admin:finance", style="danger")],
        ]
    )
    await message.answer(
        "💵 <b>Confirmar ajuste</b>\n\n"
        f"Cliente: <code>{telegram_id}</code>\n"
        f"Saldo anterior: <b>${money(current)}</b>\n"
        f"Ajuste: <b>{_signed_money(amount)}</b>\n"
        f"Saldo resultante: <b>${money(new_balance)}</b>\n"
        f"Motivo: <i>{h_truncate(reason, 1000)}</i>",
        reply_markup=markup,
    )


@router.callback_query(F.data == "admin:balance:confirm")
async def balance_confirm(
    callback: CallbackQuery,
    state: FSMContext,
    bot: Bot,
    ctx: AppContext,
) -> None:
    if not await _require_admin(callback, ctx):
        return
    if await state.get_state() != BalanceAdjustmentStates.waiting_confirmation.state:
        await callback.answer("No hay un ajuste pendiente", show_alert=True)
        return
    data = await state.get_data()
    try:
        async with ctx.session_factory() as session:
            result = await adjust_user_balance(
                session,
                telegram_id=int(data["telegram_id"]),
                amount=Decimal(str(data["amount"])),
                admin_telegram_id=callback.from_user.id,
                reason=str(data["reason"]),
            )
    except (FinanceUserNotFound, BalanceWouldBeNegative, FinanceError) as exc:
        await callback.answer(str(exc), show_alert=True)
        return

    await state.clear()
    await callback.answer("Saldo actualizado")
    action = "acreditó" if result.amount > 0 else "descontó"
    try:
        await bot.send_message(
            result.telegram_id,
            "💵 <b>Saldo actualizado</b>\n\n"
            f"Se {action}: <b>{_signed_money(result.amount)} USDT</b>\n"
            f"Nuevo saldo: <b>${money(result.balance_after)} USDT</b>\n"
            f"Motivo: <i>{h_truncate(str(data['reason']), 1000)}</i>",
        )
    except Exception:
        logger.exception("Could not notify balance-adjusted user %s", result.telegram_id)
    await answer_or_replace(
        callback,
        "✅ <b>Saldo actualizado</b>\n\n"
        f"Código: <code>{h(result.adjustment_code)}</code>\n"
        f"Cliente: <code>{result.telegram_id}</code>\n"
        f"Saldo anterior: <b>${money(result.balance_before)}</b>\n"
        f"Ajuste: <b>{_signed_money(result.amount)}</b>\n"
        f"Saldo nuevo: <b>${money(result.balance_after)}</b>",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [button("⬅️ Reembolsos y saldos", callback_data="admin:finance", style="primary")]
            ]
        ),
    )
