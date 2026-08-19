from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import select

from app.config import Settings
from app.context import AppContext
from app.database import create_engine_and_session_factory, init_database
from app.handlers.wallet import recover_pending_transaction_handler
from app.models import Deposit, User
from app.services.deposits import create_pending_deposit


class FakeStatusMessage:
    def __init__(self, text: str) -> None:
        self.text = text
        self.edits: list[str] = []

    async def edit_text(self, text: str, **_kwargs: object) -> None:
        self.edits.append(text)


class FakeCustomerMessage:
    def __init__(self, *, telegram_id: int, text: str) -> None:
        self.text = text
        self.from_user = SimpleNamespace(
            id=telegram_id,
            username="customer",
            first_name="Customer",
        )
        self.answers: list[FakeStatusMessage] = []

    async def answer(self, text: str, **_kwargs: object) -> FakeStatusMessage:
        response = FakeStatusMessage(text)
        self.answers.append(response)
        return response


class FakeBot:
    def __init__(self) -> None:
        self.notifications: list[tuple[int, str]] = []

    async def send_message(self, telegram_id: int, text: str) -> None:
        self.notifications.append((telegram_id, text))


class FakeBinance:
    def __init__(self, message: FakeCustomerMessage) -> None:
        self.message = message
        self.calls: list[dict[str, object]] = []

    async def verify_received_transaction(self, **kwargs: object) -> SimpleNamespace:
        # The customer must see a response before the external API request starts.
        assert self.message.answers
        assert "Verificando" in self.message.answers[0].text
        self.calls.append(kwargs)
        transaction_id = str(kwargs["transaction_id"])
        return SimpleNamespace(transaction_id=f"M_P_{transaction_id}", raw={"success": True})

    @staticmethod
    def serialize_raw(_payload: object) -> str:
        return '{"success":true}'


@pytest.mark.asyncio
async def test_order_id_is_recovered_and_credited_after_fsm_restart() -> None:
    engine, factory = create_engine_and_session_factory("sqlite+aiosqlite:///:memory:")
    await init_database(engine)
    telegram_id = 801
    order_id = "449526558751916032"

    async with factory() as session:
        user = User(
            telegram_id=telegram_id,
            username="customer",
            first_name="Customer",
            language="es",
            balance=Decimal("0.00"),
        )
        session.add(user)
        await session.commit()
        deposit = await create_pending_deposit(
            session,
            user_id=user.id,
            amount=Decimal("0.10"),
        )
        deposit_id = deposit.id

    storage = MemoryStorage()
    state = FSMContext(
        storage=storage,
        key=StorageKey(bot_id=1, chat_id=telegram_id, user_id=telegram_id),
    )
    assert await state.get_state() is None

    message = FakeCustomerMessage(telegram_id=telegram_id, text=order_id)
    binance = FakeBinance(message)
    bot = FakeBot()
    config = Settings(
        BOT_TOKEN="1234567890:abcdefghijklmnopqrstuvwxyzABCDE",
        ADMIN_IDS="999",
        VERIFICATION_COOLDOWN_SECONDS=0,
        BINANCE_VERIFY_ATTEMPTS=1,
    )
    ctx = AppContext(config=config, session_factory=factory, binance=binance)  # type: ignore[arg-type]

    await recover_pending_transaction_handler(  # type: ignore[arg-type]
        message,
        state,
        bot,
        ctx,
    )

    assert await state.get_state() is None
    assert len(binance.calls) == 1
    assert binance.calls[0]["transaction_id"] == order_id
    assert binance.calls[0]["expected_amount"] == Decimal("0.10")
    assert any("Pago confirmado" in edit for edit in message.answers[0].edits)

    async with factory() as session:
        saved_deposit = await session.get(Deposit, deposit_id)
        saved_user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
    assert saved_deposit is not None
    assert saved_deposit.status == "credited"
    assert saved_deposit.transaction_id == f"M_P_{order_id}"
    assert saved_user is not None
    assert saved_user.balance == Decimal("0.10")

    await storage.close()
    await engine.dispose()
