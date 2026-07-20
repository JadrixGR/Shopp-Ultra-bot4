from __future__ import annotations

import secrets
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import BalanceAdjustment, Order, Refund, User

_CENT = Decimal("0.01")


class FinanceError(Exception):
    pass


class FinanceUserNotFound(FinanceError):
    pass


class FinanceOrderNotFound(FinanceError):
    pass


class InvalidRefund(FinanceError):
    pass


class NothingToRefund(FinanceError):
    pass


class BalanceWouldBeNegative(FinanceError):
    pass


@dataclass(frozen=True, slots=True)
class RefundPreview:
    original_price: Decimal
    already_refunded: Decimal
    target_total_refund: Decimal
    amount_to_credit: Decimal
    total_days: int | None
    used_days: int | None
    remaining_days: int | None


@dataclass(frozen=True, slots=True)
class RefundResult:
    refund_id: int
    refund_code: str
    order_id: int
    order_code: str
    telegram_id: int
    product_name: str
    amount: Decimal
    total_refunded: Decimal
    new_balance: Decimal
    refund_status: str


@dataclass(frozen=True, slots=True)
class BalanceAdjustmentResult:
    adjustment_id: int
    adjustment_code: str
    telegram_id: int
    amount: Decimal
    balance_before: Decimal
    balance_after: Decimal


def _money(value: Decimal | int | str) -> Decimal:
    return Decimal(value).quantize(_CENT, rounding=ROUND_HALF_UP)


def calculate_prorated_target(
    price: Decimal,
    *,
    total_days: int,
    used_days: int,
) -> Decimal:
    """Return the cumulative refund target for unused service days.

    Example: a 30-day product bought for 20 USDT and used for 15 days has a
    target refund of 10 USDT. Previous refunds are subtracted separately.
    """

    if total_days < 1 or total_days > 3650:
        raise InvalidRefund("La duración total debe estar entre 1 y 3650 días")
    if used_days < 0 or used_days > total_days:
        raise InvalidRefund("Los días usados deben estar entre 0 y la duración total")
    remaining_days = total_days - used_days
    return _money(_money(price) * Decimal(remaining_days) / Decimal(total_days))


def build_refund_preview(
    *,
    price: Decimal,
    already_refunded: Decimal,
    refund_type: str,
    total_days: int | None = None,
    used_days: int | None = None,
) -> RefundPreview:
    original = _money(price)
    previous = max(Decimal("0.00"), _money(already_refunded))
    refundable_remaining = max(Decimal("0.00"), original - previous)
    if refundable_remaining <= 0:
        raise NothingToRefund("La compra ya fue reembolsada por completo")

    if refund_type == "full":
        target = original
        total = used = remaining = None
    elif refund_type == "prorated":
        if total_days is None or used_days is None:
            raise InvalidRefund("Faltan la duración total o los días usados")
        target = calculate_prorated_target(
            original,
            total_days=total_days,
            used_days=used_days,
        )
        total = total_days
        used = used_days
        remaining = total_days - used_days
    else:
        raise InvalidRefund("Tipo de reembolso inválido")

    amount = min(refundable_remaining, max(Decimal("0.00"), target - previous))
    amount = _money(amount)
    if amount <= 0:
        raise NothingToRefund(
            "El prorrateo calculado no genera un reembolso adicional para esta compra"
        )

    return RefundPreview(
        original_price=original,
        already_refunded=previous,
        target_total_refund=target,
        amount_to_credit=amount,
        total_days=total,
        used_days=used,
        remaining_days=remaining,
    )


def _refund_code() -> str:
    return f"REF-{secrets.token_hex(5).upper()}"


def _adjustment_code() -> str:
    return f"ADJ-{secrets.token_hex(5).upper()}"


async def refund_order(
    session: AsyncSession,
    *,
    order_id: int,
    admin_telegram_id: int,
    refund_type: str,
    total_days: int | None = None,
    used_days: int | None = None,
    reason: str = "",
) -> RefundResult:
    async with session.begin():
        order = await session.scalar(select(Order).where(Order.id == order_id).with_for_update())
        if order is None:
            raise FinanceOrderNotFound("Compra no encontrada")
        user = await session.scalar(select(User).where(User.id == order.user_id).with_for_update())
        if user is None:
            raise FinanceUserNotFound("Usuario de la compra no encontrado")

        preview = build_refund_preview(
            price=Decimal(order.price),
            already_refunded=Decimal(order.refunded_amount or 0),
            refund_type=refund_type,
            total_days=total_days,
            used_days=used_days,
        )
        before = _money(user.balance)
        after = _money(before + preview.amount_to_credit)
        total_refunded = _money(preview.already_refunded + preview.amount_to_credit)
        refund_status = "full" if total_refunded >= preview.original_price else "partial"

        refund = Refund(
            refund_code=_refund_code(),
            order_id=order.id,
            user_id=user.id,
            admin_telegram_id=admin_telegram_id,
            refund_type=refund_type,
            original_price=preview.original_price,
            total_days=preview.total_days,
            used_days=preview.used_days,
            remaining_days=preview.remaining_days,
            amount=preview.amount_to_credit,
            reason=reason.strip()[:2000],
        )
        session.add(refund)
        await session.flush()

        adjustment = BalanceAdjustment(
            adjustment_code=_adjustment_code(),
            user_id=user.id,
            admin_telegram_id=admin_telegram_id,
            amount=preview.amount_to_credit,
            balance_before=before,
            balance_after=after,
            adjustment_type="refund",
            reference_type="refund",
            reference_id=refund.id,
            reason=(reason.strip() or f"Reembolso de {order.order_code}")[:2000],
        )
        session.add(adjustment)
        user.balance = after
        order.refunded_amount = total_refunded
        order.refund_status = refund_status
        await session.flush()

        return RefundResult(
            refund_id=refund.id,
            refund_code=refund.refund_code,
            order_id=order.id,
            order_code=order.order_code,
            telegram_id=user.telegram_id,
            product_name=order.product_name,
            amount=preview.amount_to_credit,
            total_refunded=total_refunded,
            new_balance=after,
            refund_status=refund_status,
        )


async def adjust_user_balance(
    session: AsyncSession,
    *,
    telegram_id: int,
    amount: Decimal,
    admin_telegram_id: int,
    reason: str,
) -> BalanceAdjustmentResult:
    normalized = _money(amount)
    if normalized == 0:
        raise FinanceError("El ajuste no puede ser cero")

    async with session.begin():
        user = await session.scalar(
            select(User).where(User.telegram_id == telegram_id).with_for_update()
        )
        if user is None:
            raise FinanceUserNotFound("No existe un usuario registrado con ese ID de Telegram")

        before = _money(user.balance)
        after = _money(before + normalized)
        if after < 0:
            raise BalanceWouldBeNegative(
                f"El saldo quedaría negativo. Saldo actual: {before:.2f} USDT"
            )

        adjustment = BalanceAdjustment(
            adjustment_code=_adjustment_code(),
            user_id=user.id,
            admin_telegram_id=admin_telegram_id,
            amount=normalized,
            balance_before=before,
            balance_after=after,
            adjustment_type="manual_credit" if normalized > 0 else "manual_debit",
            reason=reason.strip()[:2000],
        )
        session.add(adjustment)
        user.balance = after
        await session.flush()

        return BalanceAdjustmentResult(
            adjustment_id=adjustment.id,
            adjustment_code=adjustment.adjustment_code,
            telegram_id=user.telegram_id,
            amount=normalized,
            balance_before=before,
            balance_after=after,
        )
