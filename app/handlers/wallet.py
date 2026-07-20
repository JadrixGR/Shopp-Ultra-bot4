from __future__ import annotations

import asyncio
import logging
from decimal import Decimal

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from app.context import AppContext
from app.handlers.helpers import answer_or_replace, show_main_menu
from app.keyboards import (
    cancel_keyboard,
    invalid_payment_keyboard,
    payment_keyboard,
    retry_deposit_keyboard,
    simple_back,
    wallet_keyboard,
)
from app.models import Deposit, User
from app.services.binance import (
    BinanceAPIError,
    BinanceTransactionMismatch,
    BinanceTransactionNotFound,
    extract_transaction_reference,
)
from app.services.deposits import (
    DepositAlreadyProcessed,
    DuplicateTransaction,
    cancel_pending_deposit,
    create_pending_deposit,
    credit_deposit,
    register_verification_attempt,
    seconds_since,
    set_deposit_failure,
)
from app.services.settings import format_bonus_tiers, get_store_profile
from app.services.users import get_or_create_user
from app.states import DepositStates
from app.texts import t
from app.utils import h, money, parse_money

logger = logging.getLogger(__name__)
router = Router(name="wallet")


async def _notify_admins(bot: Bot, ctx: AppContext, text: str) -> None:
    for admin_id in ctx.config.admin_ids:
        try:
            await bot.send_message(admin_id, text)
        except Exception:
            logger.exception("Could not notify admin %s", admin_id)


@router.callback_query(F.data == "wallet")
async def wallet_handler(callback: CallbackQuery, state: FSMContext, ctx: AppContext) -> None:
    await callback.answer()
    await state.clear()
    async with ctx.session_factory() as session:
        user = await get_or_create_user(session, callback.from_user)
        profile = await get_store_profile(session)
    tiers = format_bonus_tiers(profile.bonus_tiers_raw, user.language)
    await answer_or_replace(
        callback,
        t(user.language, "wallet_title", tiers=tiers),
        wallet_keyboard(user.language),
    )


@router.callback_query(F.data == "wallet:binance")
async def binance_start_handler(
    callback: CallbackQuery, state: FSMContext, ctx: AppContext
) -> None:
    async with ctx.session_factory() as session:
        user = await get_or_create_user(session, callback.from_user)
        profile = await get_store_profile(session)
    if not profile.binance_pay_id or not profile.binance_pay_name:
        await callback.answer(t(user.language, "pay_not_configured"), show_alert=True)
        return
    await callback.answer()
    await state.set_state(DepositStates.waiting_amount)
    await answer_or_replace(
        callback,
        t(user.language, "amount_prompt", minimum=money(ctx.config.min_deposit)),
        cancel_keyboard(user.language, "wallet:cancel"),
    )


@router.message(DepositStates.waiting_amount)
async def deposit_amount_handler(message: Message, state: FSMContext, ctx: AppContext) -> None:
    async with ctx.session_factory() as session:
        user = await get_or_create_user(session, message.from_user)
        profile = await get_store_profile(session)
    try:
        amount = parse_money(message.text or "")
    except ValueError:
        amount = Decimal("0")
    if amount < ctx.config.min_deposit:
        await message.answer(
            t(user.language, "amount_invalid", minimum=money(ctx.config.min_deposit)),
            reply_markup=cancel_keyboard(user.language, "wallet:cancel"),
        )
        return
    if not profile.binance_pay_id or not profile.binance_pay_name:
        await message.answer(t(user.language, "pay_not_configured"))
        await state.clear()
        return

    async with ctx.session_factory() as session:
        user = await get_or_create_user(session, message.from_user)
        deposit = await create_pending_deposit(session, user_id=user.id, amount=amount)
    await state.update_data(deposit_id=deposit.id)
    await state.set_state(DepositStates.waiting_transaction_id)
    await message.answer(
        t(
            user.language,
            "payment_instructions",
            pay_id=h(profile.binance_pay_id),
            pay_name=h(profile.binance_pay_name),
            amount=money(amount),
        ),
        reply_markup=payment_keyboard(user.language, profile.binance_pay_id),
    )


@router.callback_query(F.data == "wallet:order_help")
async def order_id_help_handler(callback: CallbackQuery, ctx: AppContext) -> None:
    async with ctx.session_factory() as session:
        user = await get_or_create_user(session, callback.from_user)
    await callback.answer()
    await callback.message.answer(t(user.language, "order_id_help"))


@router.callback_query(F.data == "wallet:cancel")
async def wallet_cancel_handler(
    callback: CallbackQuery, state: FSMContext, ctx: AppContext
) -> None:
    data = await state.get_data()
    deposit_id = data.get("deposit_id")
    if isinstance(deposit_id, int):
        async with ctx.session_factory() as session:
            await cancel_pending_deposit(session, deposit_id=deposit_id)
    await state.clear()
    async with ctx.session_factory() as session:
        user = await get_or_create_user(session, callback.from_user)
    await callback.answer(t(user.language, "cancelled"))
    await show_main_menu(callback, ctx)


@router.callback_query(F.data.startswith("wallet:cancel_deposit:"))
async def wallet_cancel_deposit_handler(
    callback: CallbackQuery,
    state: FSMContext,
    ctx: AppContext,
) -> None:
    try:
        deposit_id = int(callback.data.rsplit(":", 1)[1])
    except (TypeError, ValueError):
        await callback.answer("Solicitud inválida", show_alert=True)
        return

    async with ctx.session_factory() as session:
        user = await get_or_create_user(session, callback.from_user)
        deposit = await session.get(Deposit, deposit_id)
        if deposit is None or deposit.user_id != user.id:
            await callback.answer(t(user.language, "deposit_not_pending"), show_alert=True)
            return
        if deposit.status == "pending":
            await cancel_pending_deposit(session, deposit_id=deposit_id)

    await state.clear()
    await callback.answer(t(user.language, "deposit_cancelled"), show_alert=True)
    await show_main_menu(callback, ctx)


async def _resolve_pending_deposit(
    message: Message, state: FSMContext, ctx: AppContext
) -> tuple[Deposit | None, User, object]:
    data = await state.get_data()
    deposit_id = data.get("deposit_id")
    async with ctx.session_factory() as session:
        user = await get_or_create_user(session, message.from_user)
        profile = await get_store_profile(session)
        deposit: Deposit | None = None
        if isinstance(deposit_id, int):
            deposit = await session.get(Deposit, deposit_id)
        if deposit is None or deposit.status != "pending":
            deposit = await session.scalar(
                select(Deposit)
                .where(Deposit.user_id == user.id, Deposit.status == "pending")
                .order_by(Deposit.id.desc())
            )
    return deposit, user, profile


async def _credit_verified_deposit(
    *,
    status_message: Message,
    deposit: Deposit,
    user: User,
    profile: object,
    verified: object,
    bot: Bot,
    ctx: AppContext,
    state: FSMContext | None,
) -> None:
    try:
        async with ctx.session_factory() as session:
            result = await credit_deposit(
                session,
                deposit_id=deposit.id,
                transaction_id=verified.transaction_id,
                raw_payload=ctx.binance.serialize_raw(verified.raw),
                bonus_tiers=profile.bonus_tiers_raw,
            )
    except DuplicateTransaction:
        await status_message.edit_text(t(user.language, "payment_duplicate"))
        return
    except DepositAlreadyProcessed:
        await status_message.edit_text(t(user.language, "payment_duplicate"))
        if state is not None:
            await state.clear()
        return

    if state is not None:
        await state.clear()
    await status_message.edit_text(
        t(
            user.language,
            "payment_success",
            amount=money(result.amount),
            bonus=money(result.bonus),
            total=money(result.total),
            balance=money(result.new_balance),
        ),
        reply_markup=simple_back(user.language),
    )
    await _notify_admins(
        bot,
        ctx,
        (
            "✅ <b>Depósito acreditado automáticamente</b>\n"
            f"Depósito: <code>#{deposit.id}</code>\n"
            f"Usuario: <code>{user.telegram_id}</code>\n"
            f"Acreditado: <b>${money(result.total)}</b>\n"
            f"Transaction ID: <code>{h(verified.transaction_id)}</code>"
        ),
    )


async def _verify_deposit(
    *,
    status_message: Message,
    deposit: Deposit,
    user: User,
    profile: object,
    transaction_id: str,
    bot: Bot,
    ctx: AppContext,
    state: FSMContext | None = None,
) -> None:
    if ctx.binance is None:
        await status_message.edit_text(
            t(user.language, "payment_manual_review"),
            reply_markup=simple_back(user.language),
        )
        await _notify_admins(
            bot,
            ctx,
            (
                "🕓 <b>Depósito pendiente de revisión</b>\n"
                f"Depósito: <code>#{deposit.id}</code>\n"
                f"Usuario: <code>{user.telegram_id}</code>\n"
                f"Monto: <b>${money(deposit.requested_amount)} USDT</b>\n"
                f"ID declarado: <code>{h(transaction_id)}</code>"
            ),
        )
        if state is not None:
            await state.clear()
        return

    verified = None
    for attempt in range(ctx.config.binance_verify_attempts):
        try:
            verified = await ctx.binance.verify_received_transaction(
                transaction_id=transaction_id,
                expected_pay_id=profile.binance_pay_id,
                expected_amount=Decimal(deposit.requested_amount),
                not_before=deposit.created_at,
                force_refresh=attempt > 0,
            )
            break
        except BinanceTransactionNotFound as exc:
            if attempt + 1 < ctx.config.binance_verify_attempts:
                await asyncio.sleep(ctx.config.binance_verify_retry_delay_seconds)
                continue
            observed = ",".join(exc.observed_transaction_ids[:5])
            failure_reason = f"transaction_not_found:inspected={exc.inspected_count}"
            if observed:
                failure_reason += f":recent={observed}"
            async with ctx.session_factory() as session:
                await set_deposit_failure(
                    session,
                    deposit_id=deposit.id,
                    reason=failure_reason,
                )
            await status_message.edit_text(
                t(user.language, "payment_not_found"),
                reply_markup=retry_deposit_keyboard(user.language, deposit.id),
            )
            await _notify_admins(
                bot,
                ctx,
                (
                    "⚠️ <b>Pago no localizado en el historial de Binance</b>\n"
                    f"Depósito: <code>#{deposit.id}</code>\n"
                    f"Usuario: <code>{user.telegram_id}</code>\n"
                    f"Order ID enviado: <code>{h(transaction_id)}</code>\n"
                    f"Movimientos revisados: <b>{exc.inspected_count}</b>\n"
                    f"IDs recientes visibles para la API: <code>{h(observed or 'ninguno')}</code>"
                ),
            )
            return
        except BinanceTransactionMismatch as exc:
            async with ctx.session_factory() as session:
                await set_deposit_failure(
                    session,
                    deposit_id=deposit.id,
                    reason=f"mismatch:{exc.reason}",
                )
            await status_message.edit_text(
                t(user.language, f"payment_mismatch_{exc.reason}"),
                reply_markup=retry_deposit_keyboard(user.language, deposit.id),
            )
            return
        except BinanceAPIError as exc:
            logger.exception("Binance API failed for deposit %s", deposit.id)
            api_code = str(exc.code) if exc.code is not None else type(exc).__name__
            async with ctx.session_factory() as session:
                await set_deposit_failure(
                    session,
                    deposit_id=deposit.id,
                    reason=f"api_error:{api_code}",
                )
            await status_message.edit_text(
                t(user.language, "payment_api_error"),
                reply_markup=retry_deposit_keyboard(user.language, deposit.id),
            )
            await _notify_admins(
                bot,
                ctx,
                (
                    "⚠️ <b>Error verificando Binance</b>\n"
                    f"Depósito: <code>#{deposit.id}</code>\n"
                    f"Usuario: <code>{user.telegram_id}</code>\n"
                    f"Monto: <b>${money(deposit.requested_amount)}</b>\n"
                    f"Order ID: <code>{h(transaction_id)}</code>\n"
                    f"Error: <code>{h(exc)}</code>"
                ),
            )
            return
        except Exception as exc:
            logger.exception("Unexpected Binance verification error for deposit %s", deposit.id)
            async with ctx.session_factory() as session:
                await set_deposit_failure(
                    session,
                    deposit_id=deposit.id,
                    reason=f"internal_error:{type(exc).__name__}",
                )
            await status_message.edit_text(
                t(user.language, "payment_api_error"),
                reply_markup=retry_deposit_keyboard(user.language, deposit.id),
            )
            return

    if verified is None:
        return
    await _credit_verified_deposit(
        status_message=status_message,
        deposit=deposit,
        user=user,
        profile=profile,
        verified=verified,
        bot=bot,
        ctx=ctx,
        state=state,
    )


@router.message(DepositStates.waiting_transaction_id)
async def transaction_id_handler(
    message: Message,
    state: FSMContext,
    bot: Bot,
    ctx: AppContext,
) -> None:
    try:
        transaction_id = extract_transaction_reference(message.text or "")
    except ValueError:
        data = await state.get_data()
        raw_deposit_id = data.get("deposit_id")
        deposit_id = raw_deposit_id if isinstance(raw_deposit_id, int) else None
        async with ctx.session_factory() as session:
            user = await get_or_create_user(session, message.from_user)
        await message.answer(
            t(user.language, "payment_id_invalid"),
            reply_markup=invalid_payment_keyboard(user.language, deposit_id),
        )
        return

    deposit, user, profile = await _resolve_pending_deposit(message, state, ctx)
    if deposit is None:
        await state.clear()
        await message.answer(t(user.language, "cancelled"))
        await show_main_menu(message, ctx)
        return

    elapsed = seconds_since(deposit.last_verify_at)
    if elapsed is not None and elapsed < ctx.config.verification_cooldown_seconds:
        wait_for = max(1, int(ctx.config.verification_cooldown_seconds - elapsed))
        await message.answer(t(user.language, "verify_wait", seconds=wait_for))
        return

    async with ctx.session_factory() as session:
        await register_verification_attempt(
            session,
            deposit_id=deposit.id,
            claimed_transaction_id=transaction_id,
        )

    status_message = await message.answer(t(user.language, "checking_payment"))
    await _verify_deposit(
        status_message=status_message,
        deposit=deposit,
        user=user,
        profile=profile,
        transaction_id=transaction_id,
        bot=bot,
        ctx=ctx,
        state=state,
    )


@router.callback_query(F.data.startswith("wallet:retry:"))
async def retry_deposit_handler(
    callback: CallbackQuery,
    bot: Bot,
    ctx: AppContext,
) -> None:
    try:
        deposit_id = int(callback.data.rsplit(":", 1)[1])
    except (TypeError, ValueError):
        await callback.answer("Solicitud inválida", show_alert=True)
        return

    async with ctx.session_factory() as session:
        user = await get_or_create_user(session, callback.from_user)
        profile = await get_store_profile(session)
        deposit = await session.get(Deposit, deposit_id)
        if deposit is not None and deposit.user_id != user.id:
            deposit = None

    if deposit is None or deposit.status != "pending":
        await callback.answer(t(user.language, "deposit_not_pending"), show_alert=True)
        return
    transaction_id = (deposit.claimed_transaction_id or "").strip()
    if not transaction_id:
        await callback.answer(t(user.language, "payment_id_invalid"), show_alert=True)
        return

    elapsed = seconds_since(deposit.last_verify_at)
    if elapsed is not None and elapsed < ctx.config.verification_cooldown_seconds:
        wait_for = max(1, int(ctx.config.verification_cooldown_seconds - elapsed))
        await callback.answer(t(user.language, "verify_wait", seconds=wait_for), show_alert=True)
        return

    async with ctx.session_factory() as session:
        await register_verification_attempt(
            session,
            deposit_id=deposit.id,
            claimed_transaction_id=transaction_id,
        )

    await callback.answer()
    status_message = await callback.message.answer(t(user.language, "checking_payment"))
    await _verify_deposit(
        status_message=status_message,
        deposit=deposit,
        user=user,
        profile=profile,
        transaction_id=transaction_id,
        bot=bot,
        ctx=ctx,
    )
