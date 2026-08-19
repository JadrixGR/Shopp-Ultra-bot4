from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Deposit, User, utcnow
from app.services.binance import (
    canonical_transaction_reference,
    transaction_reference_aliases,
)
from app.services.settings import calculate_bonus
from app.texts import t

logger = logging.getLogger(__name__)

DEFAULT_DEPOSIT_EXPIRATION_MINUTES = 14
DEPOSIT_EXPIRATION_REASON = "Expired automatically at payment deadline"


class DepositError(Exception):
    pass


class DepositAlreadyProcessed(DepositError):
    pass


class DepositExpired(DepositAlreadyProcessed):
    pass


class DuplicateTransaction(DepositError):
    pass


@dataclass(frozen=True, slots=True)
class DepositCreditResult:
    amount: Decimal
    bonus: Decimal
    total: Decimal
    new_balance: Decimal


@dataclass(frozen=True, slots=True)
class ExpiredDepositNotice:
    deposit_id: int
    telegram_id: int
    language: str
    amount: Decimal


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def deposit_deadline(
    deposit: Deposit,
    *,
    expiration_minutes: int = DEFAULT_DEPOSIT_EXPIRATION_MINUTES,
) -> datetime:
    if deposit.expires_at is not None:
        return _aware(deposit.expires_at)
    return _aware(deposit.created_at) + timedelta(minutes=expiration_minutes)


def deposit_is_expired(
    deposit: Deposit,
    *,
    now: datetime | None = None,
    expiration_minutes: int = DEFAULT_DEPOSIT_EXPIRATION_MINUTES,
) -> bool:
    current = _aware(now or utcnow())
    return deposit_deadline(deposit, expiration_minutes=expiration_minutes) <= current


def deposit_expired_automatically(deposit: Deposit | None) -> bool:
    return bool(
        deposit is not None
        and deposit.status == "cancelled"
        and deposit.failure_reason == DEPOSIT_EXPIRATION_REASON
    )


async def create_pending_deposit(
    session: AsyncSession,
    *,
    user_id: int,
    amount: Decimal,
    expiration_minutes: int = DEFAULT_DEPOSIT_EXPIRATION_MINUTES,
) -> Deposit:
    if expiration_minutes < 1:
        raise ValueError("expiration_minutes must be positive")
    await session.execute(
        update(Deposit)
        .where(Deposit.user_id == user_id, Deposit.status == "pending")
        .values(status="cancelled", failure_reason="Superseded by a new request")
    )
    created_at = utcnow()
    deposit = Deposit(
        user_id=user_id,
        requested_amount=amount.quantize(Decimal("0.01")),
        currency="USDT",
        status="pending",
        created_at=created_at,
        expires_at=created_at + timedelta(minutes=expiration_minutes),
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
    if deposit_is_expired(deposit):
        deposit.status = "cancelled"
        deposit.failure_reason = DEPOSIT_EXPIRATION_REASON
        deposit.verified_at = utcnow()
        await session.commit()
        return None
    deposit.claimed_transaction_id = claimed_transaction_id.strip()
    deposit.verify_attempts += 1
    deposit.last_verify_at = utcnow()
    deposit.failure_reason = failure_reason
    await session.commit()
    return deposit


async def set_deposit_failure(session: AsyncSession, *, deposit_id: int, reason: str) -> bool:
    deposit = await session.get(Deposit, deposit_id)
    if deposit is not None and deposit.status == "pending":
        expired = deposit_is_expired(deposit)
        if expired:
            deposit.status = "cancelled"
            deposit.failure_reason = DEPOSIT_EXPIRATION_REASON
            deposit.verified_at = utcnow()
        else:
            deposit.failure_reason = reason
        await session.commit()
        return expired
    return deposit_expired_automatically(deposit)


async def expire_pending_deposit(
    session: AsyncSession,
    *,
    deposit_id: int,
    now: datetime | None = None,
) -> bool:
    current = _aware(now or utcnow())
    expired_id = await session.scalar(
        update(Deposit)
        .where(
            Deposit.id == deposit_id,
            Deposit.status == "pending",
            Deposit.expires_at <= current,
        )
        .values(
            status="cancelled",
            failure_reason=DEPOSIT_EXPIRATION_REASON,
            verified_at=current,
        )
        .returning(Deposit.id)
    )
    await session.commit()
    return expired_id is not None


async def expire_due_deposits(
    session: AsyncSession,
    *,
    now: datetime | None = None,
) -> tuple[ExpiredDepositNotice, ...]:
    current = _aware(now or utcnow())
    due = (
        await session.execute(
            select(
                Deposit.id,
                User.telegram_id,
                User.language,
                Deposit.requested_amount,
            )
            .join(User, User.id == Deposit.user_id)
            .where(
                Deposit.status == "pending",
                Deposit.expires_at <= current,
            )
            .order_by(Deposit.id)
        )
    ).all()
    if not due:
        return ()

    candidate_ids = [int(row.id) for row in due]
    expired_ids = set(
        (
            await session.scalars(
                update(Deposit)
                .where(
                    Deposit.id.in_(candidate_ids),
                    Deposit.status == "pending",
                    Deposit.expires_at <= current,
                )
                .values(
                    status="cancelled",
                    failure_reason=DEPOSIT_EXPIRATION_REASON,
                    verified_at=current,
                )
                .returning(Deposit.id)
            )
        ).all()
    )
    await session.commit()
    return tuple(
        ExpiredDepositNotice(
            deposit_id=int(row.id),
            telegram_id=int(row.telegram_id),
            language=str(row.language or "es"),
            amount=Decimal(row.requested_amount),
        )
        for row in due
        if row.id in expired_ids
    )


async def deposit_expiration_loop(
    session_factory: async_sessionmaker[AsyncSession],
    bot: Any,
    *,
    poll_interval_seconds: float = 5.0,
) -> None:
    while True:
        try:
            async with session_factory() as session:
                expired = await expire_due_deposits(session)
            for notice in expired:
                try:
                    await bot.send_message(
                        notice.telegram_id,
                        t(
                            notice.language,
                            "deposit_expired",
                            amount=f"{notice.amount:.2f}",
                        ),
                    )
                except Exception:
                    logger.exception(
                        "Could not notify user %s about expired deposit %s",
                        notice.telegram_id,
                        notice.deposit_id,
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Could not expire pending Binance deposits")
        await asyncio.sleep(max(1.0, poll_interval_seconds))


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

    expired = False
    result: DepositCreditResult | None = None
    try:
        async with session.begin():
            current = utcnow()
            claimed_id = await session.scalar(
                update(Deposit)
                .where(
                    Deposit.id == deposit_id,
                    Deposit.status == "pending",
                    Deposit.expires_at > current,
                )
                .values(status="verifying")
                .returning(Deposit.id)
            )
            deposit = await session.get(Deposit, deposit_id)
            if claimed_id is None:
                if (
                    deposit is not None
                    and deposit.status == "pending"
                    and deposit_is_expired(deposit, now=current)
                ):
                    deposit.status = "cancelled"
                    deposit.failure_reason = DEPOSIT_EXPIRATION_REASON
                    deposit.verified_at = current
                    expired = True
                else:
                    raise DepositAlreadyProcessed
            if not expired and deposit is None:
                raise DepositAlreadyProcessed
            if not expired and deposit is not None:
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
                deposit.verified_at = current
                deposit.failure_reason = None
                deposit.raw_payload = raw_payload
                await session.flush()

                result = DepositCreditResult(
                    amount=Decimal(deposit.requested_amount),
                    bonus=bonus,
                    total=total,
                    new_balance=new_balance,
                )
    except IntegrityError as exc:
        await session.rollback()
        raise DuplicateTransaction from exc
    if expired:
        raise DepositExpired
    if result is None:
        raise DepositAlreadyProcessed
    return result


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
