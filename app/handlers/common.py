from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy import func, select

from app.context import AppContext
from app.handlers.helpers import answer_or_replace, show_main_menu
from app.keyboards import (
    appearance_button,
    history_keyboard,
    language_keyboard,
    settings_activity_keyboard,
    settings_keyboard,
    store_keyboard,
    wallet_keyboard,
)
from app.models import BalanceAdjustment, Deposit, Order, Product, StockItem
from app.rich_text import ensure_html_block_before, render_rich_text
from app.services.catalog import list_active_products
from app.services.deposits import cancel_pending_deposit
from app.services.settings import format_bonus_tiers, get_store_profile
from app.services.users import get_or_create_user, set_user_language
from app.states import DepositStates
from app.texts import t
from app.ui_customization import strip_custom_emoji_entities
from app.ui_rendering import render_store_animated_preview
from app.utils import h, h_truncate, money, shorten

router = Router(name="common")


def _history_instructions_block(
    language: str,
    instructions: str,
    instructions_entities: str,
) -> str:
    if not instructions.strip():
        return ""
    title = "Instrucciones de activación" if language == "es" else "Activation instructions"
    return (
        "📋 <b>"
        + title
        + "</b>\n"
        + render_rich_text(
            instructions,
            instructions_entities,
            max_chars=1800,
        )
        + "\n\n"
    )


async def _cancel_active_flow(state: FSMContext, ctx: AppContext) -> bool:
    """Cancel a wallet request associated with the current FSM state, then clear it."""

    current_state = await state.get_state()
    data = await state.get_data()
    deposit_id = data.get("deposit_id")
    deposit_flow = current_state in {
        DepositStates.waiting_amount.state,
        DepositStates.waiting_transaction_id.state,
    }

    if isinstance(deposit_id, int):
        async with ctx.session_factory() as session:
            await cancel_pending_deposit(session, deposit_id=deposit_id)
        deposit_flow = True

    await state.clear()
    return deposit_flow


async def _notify_cancelled_flow(message: Message, ctx: AppContext, deposit_flow: bool) -> None:
    if not deposit_flow:
        return
    async with ctx.session_factory() as session:
        user = await get_or_create_user(session, message.from_user)
    await message.answer(t(user.language, "deposit_cancelled"))


@router.message(CommandStart())
async def start_handler(message: Message, state: FSMContext, ctx: AppContext) -> None:
    deposit_flow = await _cancel_active_flow(state, ctx)
    await _notify_cancelled_flow(message, ctx, deposit_flow)
    await show_main_menu(message, ctx)


@router.message(Command("menu"))
async def menu_command(message: Message, state: FSMContext, ctx: AppContext) -> None:
    deposit_flow = await _cancel_active_flow(state, ctx)
    await _notify_cancelled_flow(message, ctx, deposit_flow)
    await show_main_menu(message, ctx)


@router.callback_query(F.data == "menu")
async def menu_callback(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    deposit_flow = await _cancel_active_flow(state, ctx)
    async with ctx.session_factory() as session:
        user = await get_or_create_user(session, callback.from_user)
    if deposit_flow:
        await callback.answer(t(user.language, "deposit_cancelled"), show_alert=True)
    else:
        await callback.answer()
    await show_main_menu(callback, ctx)


@router.message(Command("cancel", "cancelar"))
async def cancel_command(message: Message, state: FSMContext, ctx: AppContext) -> None:
    deposit_flow = await _cancel_active_flow(state, ctx)
    async with ctx.session_factory() as session:
        user = await get_or_create_user(session, message.from_user)
    await message.answer(t(user.language, "deposit_cancelled" if deposit_flow else "cancelled"))
    await show_main_menu(message, ctx)


async def _show_shop(target: Message | CallbackQuery, ctx: AppContext, page: int = 0) -> None:
    del page
    async with ctx.session_factory() as session:
        user = await get_or_create_user(session, target.from_user)
        products, total = await list_active_products(session, page_size=None)
    text = t(user.language, "shop_title")
    animated_preview = render_store_animated_preview(products, user.language)
    if animated_preview:
        text += "\n\n" + t(user.language, "store_animated_preview_title") + "\n" + animated_preview
    if not products:
        text += "\n\n" + t(user.language, "shop_empty")
    await answer_or_replace(
        target,
        text,
        store_keyboard(
            user.language,
            products,
            page=0,
            total=total,
            page_size=None,
        ),
    )


@router.message(Command("tienda", "shop"))
async def shop_command(message: Message, state: FSMContext, ctx: AppContext) -> None:
    deposit_flow = await _cancel_active_flow(state, ctx)
    await _notify_cancelled_flow(message, ctx, deposit_flow)
    await _show_shop(message, ctx)


@router.message(Command("wallet", "recargar"))
async def wallet_command(message: Message, state: FSMContext, ctx: AppContext) -> None:
    deposit_flow = await _cancel_active_flow(state, ctx)
    await _notify_cancelled_flow(message, ctx, deposit_flow)
    async with ctx.session_factory() as session:
        user = await get_or_create_user(session, message.from_user)
        profile = await get_store_profile(session)
    tiers = format_bonus_tiers(profile.bonus_tiers_raw, user.language)
    await answer_or_replace(
        message,
        t(user.language, "wallet_title", tiers=tiers),
        wallet_keyboard(user.language),
    )


async def _history_data(target: Message | CallbackQuery, ctx: AppContext):
    async with ctx.session_factory() as session:
        user = await get_or_create_user(session, target.from_user)
        order_rows = (
            await session.execute(
                select(Order, StockItem.payload)
                .join(StockItem, StockItem.id == Order.stock_item_id)
                .where(Order.user_id == user.id)
                .order_by(Order.id.desc())
                .limit(8)
            )
        ).all()
        deposits = (
            await session.scalars(
                select(Deposit)
                .where(Deposit.user_id == user.id)
                .order_by(Deposit.id.desc())
                .limit(5)
            )
        ).all()
        adjustments = (
            await session.scalars(
                select(BalanceAdjustment)
                .where(BalanceAdjustment.user_id == user.id)
                .order_by(BalanceAdjustment.id.desc())
                .limit(6)
            )
        ).all()
    return user, order_rows, deposits, adjustments


async def _show_history(target: Message | CallbackQuery, ctx: AppContext) -> None:
    user, order_rows, deposits, adjustments = await _history_data(target, ctx)

    text = t(user.language, "history_title")
    if order_rows:
        rendered_orders: list[str] = []
        for order, payload in order_rows:
            preview = shorten(" ".join(str(payload).splitlines()), 80)
            refund_line = ""
            if Decimal(order.refunded_amount or 0) > 0:
                refund_label = "Refunded" if user.language == "en" else "Reembolsado"
                refund_line = f"\n  💸 {refund_label}: <b>${money(order.refunded_amount)}</b>"
            rendered_orders.append(
                f"• <code>{h(order.order_code)}</code> — "
                f"{h_truncate(shorten(order.product_name, 45), 170)} — ${money(order.price)}\n"
                f"  📦 <code>{h_truncate(preview, 240)}</code>{refund_line}"
            )
        text += t(user.language, "history_orders", items="\n".join(rendered_orders))
    if deposits:
        status_names = {
            "pending": "🕓",
            "credited": "✅",
            "rejected": "❌",
            "cancelled": "⚪",
        }
        deposit_items = "\n".join(
            f"• {status_names.get(dep.status, '•')} ${money(dep.requested_amount)} — {h(dep.status)}"
            for dep in deposits
        )
        text += t(user.language, "history_deposits", items=deposit_items)
    if adjustments:
        labels_es = {
            "refund": "Reembolso",
            "manual_credit": "Crédito administrativo",
            "manual_debit": "Débito administrativo",
        }
        labels_en = {
            "refund": "Refund",
            "manual_credit": "Administrative credit",
            "manual_debit": "Administrative debit",
        }
        labels = labels_en if user.language == "en" else labels_es
        movement_lines = []
        for item in adjustments:
            amount = Decimal(item.amount)
            amount_text = f"+${money(amount)}" if amount > 0 else f"-${money(abs(amount))}"
            label = labels.get(item.adjustment_type, item.adjustment_type)
            movement_lines.append(
                f"• <b>{amount_text}</b> — {h(label)}\n"
                f"  <i>{h_truncate(item.reason or '—', 180)}</i>"
            )
        heading = "Balance movements" if user.language == "en" else "Movimientos de saldo"
        text += f"\n\n💵 <b>{heading}</b>\n" + "\n".join(movement_lines)
    if not order_rows and not deposits and not adjustments:
        text += "\n\n" + t(user.language, "history_empty")

    buttons = [(order.id, order.order_code, order.product_name) for order, _payload in order_rows]
    await answer_or_replace(target, text, history_keyboard(user.language, buttons))


@router.callback_query(F.data == "history")
async def history_handler(callback: CallbackQuery, ctx: AppContext) -> None:
    await callback.answer()
    await _show_history(callback, ctx)


@router.message(Command("historial", "history"))
async def history_command(message: Message, state: FSMContext, ctx: AppContext) -> None:
    deposit_flow = await _cancel_active_flow(state, ctx)
    await _notify_cancelled_flow(message, ctx, deposit_flow)
    await _show_history(message, ctx)


@router.callback_query(F.data.startswith("history:order:"))
async def history_order_delivery(callback: CallbackQuery, ctx: AppContext) -> None:
    try:
        order_id = int(callback.data.rsplit(":", 1)[1])
    except (TypeError, ValueError):
        await callback.answer("Solicitud inválida", show_alert=True)
        return

    async with ctx.session_factory() as session:
        user = await get_or_create_user(session, callback.from_user)
        row = (
            await session.execute(
                select(
                    Order,
                    StockItem.payload,
                    Product.instructions,
                    Product.instructions_entities,
                )
                .join(StockItem, StockItem.id == Order.stock_item_id)
                .join(Product, Product.id == Order.product_id)
                .where(Order.id == order_id, Order.user_id == user.id)
            )
        ).first()

    if row is None:
        await callback.answer(t(user.language, "history_order_not_found"), show_alert=True)
        return

    order, payload, current_instructions, current_instructions_entities = row
    instructions = order.instructions_snapshot or current_instructions or ""
    instructions_entities = (
        order.instructions_entities_snapshot
        if order.instructions_snapshot
        else current_instructions_entities or "[]"
    )
    instructions_block = _history_instructions_block(
        user.language,
        instructions,
        instructions_entities,
    )
    date_text = order.created_at.strftime("%Y-%m-%d %H:%M UTC")
    delivery = t(
        user.language,
        "history_delivery",
        order=h(order.order_code),
        name=h_truncate(order.product_name, 300),
        price=money(order.price),
        date=h(date_text),
        instructions_block=instructions_block,
        payload=h(payload),
    )
    delivery = ensure_html_block_before(
        delivery,
        instructions_block,
        markers=("<b>Contenido adquirido:</b>", "<b>Purchased content:</b>"),
    )
    refund_suffix = ""
    if Decimal(order.refunded_amount or 0) > 0:
        refund_label = "Refunded" if user.language == "en" else "Reembolsado"
        refund_suffix = (
            f"\n\n💸 <b>{refund_label}:</b> ${money(order.refunded_amount)} "
            f"({h(order.refund_status)})"
        )
        delivery += refund_suffix
    await callback.answer(t(user.language, "history_resend_notice"))
    if callback.message is None:
        return
    if len(delivery) <= 4000:
        try:
            await callback.message.answer(delivery)
        except TelegramBadRequest:
            if "<tg-emoji" not in delivery:
                raise
            await callback.message.answer(strip_custom_emoji_entities(delivery))
        return

    summary = t(
        user.language,
        "history_delivery_file",
        order=h(order.order_code),
        name=h_truncate(order.product_name, 300),
        price=money(order.price),
        date=h(date_text),
        instructions_block=instructions_block,
    )
    summary = ensure_html_block_before(
        summary,
        instructions_block,
        markers=("El contenido se adjunta", "The content is attached"),
    )
    summary += refund_suffix
    try:
        await callback.message.answer(summary)
    except TelegramBadRequest:
        if "<tg-emoji" not in summary:
            raise
        await callback.message.answer(strip_custom_emoji_entities(summary))
    await callback.message.answer_document(
        BufferedInputFile(str(payload).encode("utf-8"), filename=f"{order.order_code}.txt"),
        caption=t(user.language, "product_file_caption", order=h(order.order_code)),
    )


@router.callback_query(F.data == "noop")
async def noop(callback: CallbackQuery, ctx: AppContext) -> None:
    async with ctx.session_factory() as session:
        user = await get_or_create_user(session, callback.from_user)
    await callback.answer(t(user.language, "noop"))


@router.callback_query(F.data == "language")
async def language_handler(callback: CallbackQuery, ctx: AppContext) -> None:
    await callback.answer()
    async with ctx.session_factory() as session:
        user = await get_or_create_user(session, callback.from_user)
    await answer_or_replace(callback, t(user.language, "language_title"), language_keyboard())


@router.callback_query(F.data.startswith("setlang:"))
async def set_language_handler(callback: CallbackQuery, ctx: AppContext) -> None:
    language = callback.data.rsplit(":", 1)[1]
    if language not in {"es", "en"}:
        await callback.answer("Invalid language", show_alert=True)
        return
    async with ctx.session_factory() as session:
        await get_or_create_user(session, callback.from_user)
        await set_user_language(session, callback.from_user.id, language)
    await callback.answer(t(language, "language_changed"))
    await show_main_menu(callback, ctx)


def _utc_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _relative_time(value: datetime | None, language: str) -> str:
    normalized = _utc_datetime(value)
    if normalized is None:
        return "—"
    seconds = max(0, int((datetime.now(UTC) - normalized).total_seconds()))
    if seconds < 60:
        return "ahora" if language == "es" else "now"
    if seconds < 3600:
        amount, suffix = seconds // 60, "m"
    elif seconds < 86400:
        amount, suffix = seconds // 3600, "h"
    else:
        amount, suffix = seconds // 86400, "d"
    return f"hace {amount}{suffix}" if language == "es" else f"{amount}{suffix} ago"


def _full_relative_time(value: datetime | None, language: str) -> str:
    normalized = _utc_datetime(value)
    if normalized is None:
        return "—"
    return f"{_relative_time(normalized, language)} ({normalized.strftime('%d %b %Y, %H:%M UTC')})"


async def _show_settings(target: Message | CallbackQuery, ctx: AppContext) -> None:
    async with ctx.session_factory() as session:
        user = await get_or_create_user(session, target.from_user)
        statistics = (
            await session.execute(
                select(
                    func.count(Order.id),
                    func.coalesce(func.sum(Order.quantity), 0),
                    func.coalesce(func.sum(Order.price), 0),
                    func.max(Order.created_at),
                ).where(Order.user_id == user.id, Order.status == "completed")
            )
        ).one()
        topups = await session.scalar(
            select(func.coalesce(func.sum(Deposit.credited_amount), 0)).where(
                Deposit.user_id == user.id,
                Deposit.status == "credited",
            )
        )

    orders_count, items_count, spent, last_order = statistics
    text = t(
        user.language,
        "settings_statistics",
        orders=int(orders_count or 0),
        items=int(items_count or 0),
        spent=money(spent or 0),
        last_order=h(_full_relative_time(last_order, user.language)),
        topups=money(topups or 0),
    )
    await answer_or_replace(target, text, settings_keyboard(user.language))


def _deposit_activity_block(deposit: Deposit, language: str) -> str:
    status_es = {
        "pending": "Pendiente",
        "credited": "Acreditado",
        "rejected": "Rechazado",
        "cancelled": "Cancelado",
    }
    status_en = {
        "pending": "Pending",
        "credited": "Credited",
        "rejected": "Rejected",
        "cancelled": "Cancelled",
    }
    labels = status_en if language == "en" else status_es
    status = labels.get(deposit.status, deposit.status)
    if language == "en":
        body = (
            f"#{deposit.id}\n"
            f"Amount: {money(deposit.requested_amount)} USDT\n"
            "Method: Binance Pay\n"
            f"Status: {h(status)}\n"
            f"Date: {_relative_time(deposit.created_at, language)}"
        )
    else:
        body = (
            f"#{deposit.id}\n"
            f"Monto: {money(deposit.requested_amount)} USDT\n"
            "Método: Binance Pay\n"
            f"Estado: {h(status)}\n"
            f"Fecha: {_relative_time(deposit.created_at, language)}"
        )
    return f"<blockquote>{body}</blockquote>"


def _order_activity_block(order: Order, language: str) -> str:
    quantity_line = ""
    if int(order.quantity or 1) > 1:
        label = "Quantity" if language == "en" else "Cantidad"
        quantity_line = f"\n{label}: {int(order.quantity)}"
    if language == "en":
        body = (
            f"#{order.id}\n"
            f"Product: {h_truncate(order.product_name, 220)}{quantity_line}\n"
            f"Amount: -{money(order.price)} USDT\n"
            "Method: Wallet balance\n"
            "Status: Paid\n"
            f"Date: {_relative_time(order.created_at, language)}"
        )
    else:
        body = (
            f"#{order.id}\n"
            f"Producto: {h_truncate(order.product_name, 220)}{quantity_line}\n"
            f"Monto: -{money(order.price)} USDT\n"
            "Método: Balance de wallet\n"
            "Estado: Pagado\n"
            f"Fecha: {_relative_time(order.created_at, language)}"
        )
    return f"<blockquote>{body}</blockquote>"


async def _show_settings_activity(target: Message | CallbackQuery, ctx: AppContext) -> None:
    async with ctx.session_factory() as session:
        user = await get_or_create_user(session, target.from_user)
        deposits = list(
            (
                await session.scalars(
                    select(Deposit)
                    .where(Deposit.user_id == user.id)
                    .order_by(Deposit.id.desc())
                    .limit(10)
                )
            ).all()
        )
        orders = list(
            (
                await session.scalars(
                    select(Order)
                    .where(Order.user_id == user.id, Order.status == "completed")
                    .order_by(Order.id.desc())
                    .limit(10)
                )
            ).all()
        )

    sections = [t(user.language, "settings_activity_title")]
    sections.append("\n\n" + t(user.language, "settings_deposits_heading"))
    if deposits:
        for deposit in deposits:
            candidate = "\n\n".join(sections + [_deposit_activity_block(deposit, user.language)])
            if len(candidate) > 3900:
                break
            sections.append(_deposit_activity_block(deposit, user.language))
    else:
        sections.append(t(user.language, "settings_no_deposits"))

    purchase_heading = t(user.language, "settings_purchases_heading")
    if len("\n\n".join(sections + [purchase_heading])) < 3900:
        sections.append(purchase_heading)
        if orders:
            for order in orders:
                block = _order_activity_block(order, user.language)
                candidate = "\n\n".join(sections + [block])
                if len(candidate) > 3900:
                    break
                sections.append(block)
        else:
            sections.append(t(user.language, "settings_no_purchases"))

    await answer_or_replace(
        target,
        "\n\n".join(sections),
        settings_activity_keyboard(user.language),
    )


@router.callback_query(F.data == "settings")
async def settings_handler(callback: CallbackQuery, ctx: AppContext) -> None:
    await callback.answer()
    await _show_settings(callback, ctx)


@router.callback_query(F.data == "settings:activity")
async def settings_activity_handler(callback: CallbackQuery, ctx: AppContext) -> None:
    await callback.answer()
    await _show_settings_activity(callback, ctx)


@router.message(Command("ajustes", "settings"))
async def settings_command(message: Message, state: FSMContext, ctx: AppContext) -> None:
    deposit_flow = await _cancel_active_flow(state, ctx)
    await _notify_cancelled_flow(message, ctx, deposit_flow)
    await _show_settings(message, ctx)


async def _show_support(target: Message | CallbackQuery, ctx: AppContext) -> None:
    async with ctx.session_factory() as session:
        user = await get_or_create_user(session, target.from_user)
        profile = await get_store_profile(session)
    if profile.support_username:
        support_url = f"https://t.me/{profile.support_username}"
    else:
        support_url = f"tg://user?id={ctx.config.primary_admin_id}"
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [appearance_button("support_contact", user.language, url=support_url)],
            [appearance_button("nav_back", user.language, callback_data="menu")],
        ]
    )
    await answer_or_replace(target, t(user.language, "support_title"), markup)


@router.callback_query(F.data == "support")
async def support_handler(callback: CallbackQuery, ctx: AppContext) -> None:
    await callback.answer()
    await _show_support(callback, ctx)


@router.message(Command("soporte", "support"))
async def support_command(message: Message, state: FSMContext, ctx: AppContext) -> None:
    deposit_flow = await _cancel_active_flow(state, ctx)
    await _notify_cancelled_flow(message, ctx, deposit_flow)
    await _show_support(message, ctx)
