from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Deposit, User, utcnow
from app.services.binance import (
    canonical_transaction_reference,
    transaction_reference_aliases,
)
from app.services.settings import calculate_bonus


class DepositError(Exception):
    pass


class DepositAlreadyProcessed(DepositError):
    pass


class DuplicateTransaction(DepositError):
    pass


@dataclass(frozen=True, slots=True)
class DepositCreditResult:
    amount: Decimal
    bonus: Decimal
    total: Decimal
    new_balance: Decimal


async def create_pending_deposit(
    session: AsyncSession, *, user_id: int, amount: Decimal
) -> Deposit:
    await session.execute(
        update(Deposit)
        .where(Deposit.user_id == user_id, Deposit.status == "pending")
        .values(status="cancelled", failure_reason="Superseded by a new request")
    )
    deposit = Deposit(
        user_id=user_id,
        requested_amount=amount.quantize(Decimal("0.01")),
        currency="USDT",
        status="pending",
    )
    session.add(deposit)
    await session.commit()
    await session.refresh(deposit)
    return deposit


async def register_verification_attempt(
    session: AsyncSession,
    *,
    deposit_id: int,
    claimed_transaction_id: str,
    failure_reason: str | None = None,
) -> Deposit | None:
    deposit = await session.get(Deposit, deposit_id)
    if deposit is None or deposit.status != "pending":
        return None
    deposit.claimed_transaction_id = claimed_transaction_id.strip()
    deposit.verify_attempts += 1
    deposit.last_verify_at = utcnow()
    deposit.failure_reason = failure_reason
    await session.commit()
    return deposit


async def set_deposit_failure(session: AsyncSession, *, deposit_id: int, reason: str) -> None:
    deposit = await session.get(Deposit, deposit_id)
    if deposit is not None and deposit.status == "pending":
        deposit.failure_reason = reason
        await session.commit()


async def credit_deposit(
    session: AsyncSession,
    *,
    deposit_id: int,
    transaction_id: str,
    raw_payload: str | None,
    bonus_tiers: str,
) -> DepositCreditResult:
    try:
        canonical_id = canonical_transaction_reference(transaction_id)
        aliases = tuple(transaction_reference_aliases(canonical_id))
    except ValueError:
        canonical_id = transaction_id.strip().upper()
        aliases = (canonical_id,)

    try:
        async with session.begin():
            deposit = await session.get(Deposit, deposit_id)
            if deposit is None or deposit.status != "pending":
                raise DepositAlreadyProcessed

            duplicate_id = await session.scalar(
                select(Deposit.id)
                .where(
                    Deposit.id != deposit_id,
                    Deposit.status == "credited",
                    or_(
                        Deposit.transaction_id.in_(aliases),
                        Deposit.claimed_transaction_id.in_(aliases),
                    ),
                )
                .limit(1)
            )
            if duplicate_id is not None:
                raise DuplicateTransaction

            user = await session.get(User, deposit.user_id)
            if user is None:
                raise DepositError("User not found")

            _percent, bonus = calculate_bonus(Decimal(deposit.requested_amount), bonus_tiers)
            total = (Decimal(deposit.requested_amount) + bonus).quantize(Decimal("0.01"))
            balance_result = await session.execute(
                update(User)
                .where(User.id == user.id)
                .values(balance=User.balance + total)
                .returning(User.balance)
            )
            new_balance = Decimal(balance_result.scalar_one())

            deposit.status = "credited"
            deposit.transaction_id = canonical_id
            deposit.claimed_transaction_id = canonical_id
            deposit.credited_amount = total
            deposit.bonus_amount = bonus
            deposit.verified_at = utcnow()
            deposit.failure_reason = None
            deposit.raw_payload = raw_payload
            await session.flush()

            return DepositCreditResult(
                amount=Decimal(deposit.requested_amount),
                bonus=bonus,
                total=total,
                new_balance=new_balance,
            )
    except IntegrityError as exc:
        await session.rollback()
        raise DuplicateTransaction from exc


async def reject_deposit(session: AsyncSession, *, deposit_id: int, reason: str) -> Deposit | None:
    deposit = await session.get(Deposit, deposit_id)
    if deposit is None or deposit.status != "pending":
        return None
    deposit.status = "rejected"
    deposit.failure_reason = reason
    deposit.verified_at = utcnow()
    await session.commit()
    return deposit


async def cancel_pending_deposit(session: AsyncSession, *, deposit_id: int) -> None:
    deposit = await session.get(Deposit, deposit_id)
    if deposit is not None and deposit.status == "pending":
        deposit.status = "cancelled"
        deposit.failure_reason = "Cancelled by user"
        await session.commit()


def seconds_since(value: datetime | None) -> float | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return (datetime.now(UTC) - value).total_seconds()
