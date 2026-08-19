from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.config import Settings
from app.database import create_engine_and_session_factory, init_database
from app.models import Deposit, User
from app.services.deposits import (
    DEPOSIT_EXPIRATION_REASON,
    DepositExpired,
    create_pending_deposit,
    credit_deposit,
    deposit_expiration_loop,
    expire_due_deposits,
    register_verification_attempt,
)
from app.texts import t


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


async def _user_and_deposit(factory, *, telegram_id: int = 81001):  # type: ignore[no-untyped-def]
    async with factory() as session:
        user = User(
            telegram_id=telegram_id,
            first_name="Deposit timer",
            balance=Decimal("0.00"),
            language="es",
        )
        session.add(user)
        await session.commit()
        deposit = await create_pending_deposit(
            session,
            user_id=user.id,
            amount=Decimal("10.00"),
            expiration_minutes=14,
        )
        return user.id, deposit.id, _aware(deposit.created_at), _aware(deposit.expires_at)


@pytest.mark.asyncio
async def test_new_deposit_has_exact_fourteen_minute_deadline() -> None:
    engine, factory = create_engine_and_session_factory("sqlite+aiosqlite:///:memory:")
    await init_database(engine)

    _user_id, _deposit_id, created_at, expires_at = await _user_and_deposit(factory)

    assert expires_at - created_at == timedelta(minutes=14)
    await engine.dispose()


@pytest.mark.asyncio
async def test_deposit_stays_pending_before_deadline_and_expires_at_exact_deadline() -> None:
    engine, factory = create_engine_and_session_factory("sqlite+aiosqlite:///:memory:")
    await init_database(engine)
    _user_id, deposit_id, _created_at, expires_at = await _user_and_deposit(factory)

    async with factory() as session:
        before = await expire_due_deposits(session, now=expires_at - timedelta(microseconds=1))
    async with factory() as session:
        pending = await session.get(Deposit, deposit_id)
        assert pending is not None and pending.status == "pending"

    async with factory() as session:
        at_deadline = await expire_due_deposits(session, now=expires_at)
    async with factory() as session:
        repeated = await expire_due_deposits(session, now=expires_at + timedelta(seconds=1))
        saved = await session.get(Deposit, deposit_id)

    assert before == ()
    assert len(at_deadline) == 1
    assert at_deadline[0].deposit_id == deposit_id
    assert repeated == ()
    assert saved is not None
    assert saved.status == "cancelled"
    assert saved.failure_reason == DEPOSIT_EXPIRATION_REASON
    await engine.dispose()


@pytest.mark.asyncio
async def test_expired_deposit_rejects_verification_attempt() -> None:
    engine, factory = create_engine_and_session_factory("sqlite+aiosqlite:///:memory:")
    await init_database(engine)
    _user_id, deposit_id, _created_at, _expires_at = await _user_and_deposit(factory)

    async with factory() as session:
        deposit = await session.get(Deposit, deposit_id)
        assert deposit is not None
        deposit.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()

    async with factory() as session:
        registered = await register_verification_attempt(
            session,
            deposit_id=deposit_id,
            claimed_transaction_id="442692493004005376",
        )
    async with factory() as session:
        saved = await session.get(Deposit, deposit_id)

    assert registered is None
    assert saved is not None
    assert saved.status == "cancelled"
    assert saved.verify_attempts == 0
    assert saved.claimed_transaction_id is None
    await engine.dispose()


@pytest.mark.asyncio
async def test_late_verified_payment_cannot_credit_customer_balance() -> None:
    engine, factory = create_engine_and_session_factory("sqlite+aiosqlite:///:memory:")
    await init_database(engine)
    user_id, deposit_id, _created_at, _expires_at = await _user_and_deposit(factory)

    async with factory() as session:
        deposit = await session.get(Deposit, deposit_id)
        assert deposit is not None
        deposit.expires_at = datetime.now(UTC) - timedelta(milliseconds=1)
        await session.commit()

    async with factory() as session:
        with pytest.raises(DepositExpired):
            await credit_deposit(
                session,
                deposit_id=deposit_id,
                transaction_id="442692493004005376",
                raw_payload="{}",
                bonus_tiers="50:2",
            )

    async with factory() as session:
        user = await session.get(User, user_id)
        deposit = await session.get(Deposit, deposit_id)
    assert user is not None and user.balance == Decimal("0.00")
    assert deposit is not None
    assert deposit.status == "cancelled"
    assert deposit.transaction_id is None
    assert deposit.credited_amount == Decimal("0.00")
    await engine.dispose()


@pytest.mark.asyncio
async def test_payment_can_still_be_credited_before_deadline() -> None:
    engine, factory = create_engine_and_session_factory("sqlite+aiosqlite:///:memory:")
    await init_database(engine)
    user_id, deposit_id, _created_at, _expires_at = await _user_and_deposit(factory)

    async with factory() as session:
        result = await credit_deposit(
            session,
            deposit_id=deposit_id,
            transaction_id="442692493004005376",
            raw_payload="{}",
            bonus_tiers="",
        )
    async with factory() as session:
        user = await session.get(User, user_id)
        deposit = await session.get(Deposit, deposit_id)

    assert result.total == Decimal("10.00")
    assert user is not None and user.balance == Decimal("10.00")
    assert deposit is not None and deposit.status == "credited"
    await engine.dispose()


@pytest.mark.asyncio
async def test_expiration_loop_cancels_and_notifies_only_once() -> None:
    engine, factory = create_engine_and_session_factory("sqlite+aiosqlite:///:memory:")
    await init_database(engine)
    _user_id, deposit_id, _created_at, _expires_at = await _user_and_deposit(factory)
    async with factory() as session:
        deposit = await session.get(Deposit, deposit_id)
        assert deposit is not None
        deposit.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()

    class NotificationBot:
        def __init__(self) -> None:
            self.messages: list[tuple[int, str]] = []

        async def send_message(self, telegram_id: int, message: str) -> None:
            self.messages.append((telegram_id, message))

    bot = NotificationBot()
    task = asyncio.create_task(
        deposit_expiration_loop(
            factory,
            bot,
            poll_interval_seconds=1,
        )
    )
    try:
        for _attempt in range(50):
            if bot.messages:
                break
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert len(bot.messages) == 1
    assert bot.messages[0][0] == 81001
    assert "venció" in bot.messages[0][1]
    async with factory() as session:
        assert await expire_due_deposits(session) == ()
    await engine.dispose()


@pytest.mark.asyncio
async def test_overdue_deposit_is_expired_after_database_restart(tmp_path) -> None:
    database_path = tmp_path / "restart.db"
    database_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    first_engine, first_factory = create_engine_and_session_factory(database_url)
    await init_database(first_engine)
    _user_id, deposit_id, _created_at, _expires_at = await _user_and_deposit(first_factory)
    async with first_factory() as session:
        deposit = await session.get(Deposit, deposit_id)
        assert deposit is not None
        deposit.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    await first_engine.dispose()

    second_engine, second_factory = create_engine_and_session_factory(database_url)
    await init_database(second_engine)
    async with second_factory() as session:
        expired = await expire_due_deposits(session)
    async with second_factory() as session:
        saved = await session.get(Deposit, deposit_id)

    assert len(expired) == 1
    assert saved is not None and saved.status == "cancelled"
    await second_engine.dispose()


@pytest.mark.asyncio
async def test_sqlite_migration_backfills_legacy_deposit_deadline(tmp_path) -> None:
    database_path = tmp_path / "legacy.db"
    connection = sqlite3.connect(database_path)
    try:
        connection.executescript(
            """
            CREATE TABLE deposits (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                requested_amount NUMERIC(18, 2) NOT NULL,
                credited_amount NUMERIC(18, 2) NOT NULL DEFAULT 0,
                bonus_amount NUMERIC(18, 2) NOT NULL DEFAULT 0,
                currency VARCHAR(12) NOT NULL DEFAULT 'USDT',
                status VARCHAR(16) NOT NULL DEFAULT 'pending',
                claimed_transaction_id VARCHAR(160),
                transaction_id VARCHAR(160),
                verify_attempts INTEGER NOT NULL DEFAULT 0,
                last_verify_at DATETIME,
                failure_reason TEXT,
                raw_payload TEXT,
                created_at DATETIME NOT NULL,
                verified_at DATETIME
            );
            INSERT INTO deposits (
                id, user_id, requested_amount, status, created_at
            ) VALUES (
                1, 1, 10.00, 'pending', '2026-08-18 12:00:00'
            );
            """
        )
        connection.commit()
    finally:
        connection.close()

    engine, _factory = create_engine_and_session_factory(
        f"sqlite+aiosqlite:///{database_path.as_posix()}"
    )
    await init_database(engine)
    async with engine.connect() as db:
        columns = {
            str(row[1]) for row in (await db.execute(text("PRAGMA table_info(deposits)"))).all()
        }
        expires_at = await db.scalar(text("SELECT expires_at FROM deposits WHERE id = 1"))

    assert "expires_at" in columns
    assert str(expires_at) == "2026-08-18 12:14:00"
    await engine.dispose()


def test_deposit_expiration_defaults_to_fourteen_minutes_and_is_validated() -> None:
    settings = Settings(
        BOT_TOKEN="1234567890:abcdefghijklmnopqrstuvwxyzABCDE",
        ADMIN_IDS="123456789",
    )
    assert settings.deposit_expiration_minutes == 14

    with pytest.raises(ValueError, match="DEPOSIT_EXPIRATION_MINUTES"):
        Settings(
            BOT_TOKEN="1234567890:abcdefghijklmnopqrstuvwxyzABCDE",
            ADMIN_IDS="123456789",
            DEPOSIT_EXPIRATION_MINUTES=0,
        )


def test_customer_is_told_about_the_fourteen_minute_deadline() -> None:
    spanish = t("es", "deposit_deadline_notice", minutes=14)
    english = t("en", "deposit_deadline_notice", minutes=14)

    assert "14 minutos" in spanish
    assert "cancelará automáticamente" in spanish
    assert "14 minutes" in english
    assert "cancelled automatically" in english
