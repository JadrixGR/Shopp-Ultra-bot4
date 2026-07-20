from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from io import BytesIO
from types import SimpleNamespace

import pytest
from aiogram.enums import ChatType
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Chat, Document, MenuButtonCommands, Message
from aiogram.types import User as TelegramUser
from sqlalchemy import func, select

from app.database import create_engine_and_session_factory, init_database
from app.handlers.common import _cancel_active_flow
from app.keyboards import history_keyboard, retry_deposit_keyboard
from app.main import configure_bot
from app.models import Deposit, Order, Product, StockItem, User
from app.services.catalog import add_stock_items
from app.services.deposits import create_pending_deposit
from app.services.notifications import broadcast_stock_update
from app.services.purchases import purchase_product
from app.services.stock_import import extract_stock_payloads
from app.states import DepositStates


class ConfigurationBot:
    def __init__(self) -> None:
        self.command_calls: list[tuple[object, dict[str, object]]] = []
        self.menu_button: object | None = None

    async def set_my_commands(self, commands: object, **kwargs: object) -> None:
        self.command_calls.append((commands, kwargs))

    async def set_chat_menu_button(self, *, menu_button: object) -> None:
        self.menu_button = menu_button


@pytest.mark.asyncio
async def test_bot_configuration_forces_native_command_menu_button() -> None:
    bot = ConfigurationBot()

    await configure_bot(bot)  # type: ignore[arg-type]

    assert len(bot.command_calls) == 2
    assert isinstance(bot.menu_button, MenuButtonCommands)
    spanish_commands = {item.command for item in bot.command_calls[0][0]}
    assert {"start", "menu", "tienda", "wallet", "historial", "cancel"}.issubset(spanish_commands)


def test_payment_retry_keyboard_has_explicit_cancel_transaction() -> None:
    markup = retry_deposit_keyboard("es", 17)

    assert markup.inline_keyboard[0][0].callback_data == "wallet:retry:17"
    assert markup.inline_keyboard[1][0].callback_data == "wallet:cancel_deposit:17"
    assert "Cancelar transacción" in markup.inline_keyboard[1][0].text


def test_history_keyboard_can_resend_each_saved_delivery() -> None:
    markup = history_keyboard("es", [(8, "ORD-TEST", "Correo premium")])

    assert markup.inline_keyboard[0][0].callback_data == "history:order:8"
    assert markup.inline_keyboard[-1][0].callback_data == "menu"


@pytest.mark.asyncio
async def test_start_or_cancel_flow_marks_pending_deposit_cancelled() -> None:
    engine, factory = create_engine_and_session_factory("sqlite+aiosqlite:///:memory:")
    await init_database(engine)

    async with factory() as session:
        user = User(
            telegram_id=501,
            first_name="Buyer",
            balance=Decimal("0.00"),
            language="es",
        )
        session.add(user)
        await session.commit()
        deposit = await create_pending_deposit(
            session,
            user_id=user.id,
            amount=Decimal("20.00"),
        )

    storage = MemoryStorage()
    state = FSMContext(
        storage=storage,
        key=StorageKey(bot_id=1, chat_id=501, user_id=501),
    )
    await state.set_state(DepositStates.waiting_transaction_id)
    await state.update_data(deposit_id=deposit.id)
    ctx = SimpleNamespace(session_factory=factory)

    was_deposit = await _cancel_active_flow(state, ctx)  # type: ignore[arg-type]

    assert was_deposit is True
    assert await state.get_state() is None
    async with factory() as session:
        saved = await session.get(Deposit, deposit.id)
        assert saved is not None
        assert saved.status == "cancelled"

    await storage.close()
    await engine.dispose()


@pytest.mark.asyncio
async def test_bulk_stock_supports_large_batches_and_counts_duplicates() -> None:
    engine, factory = create_engine_and_session_factory("sqlite+aiosqlite:///:memory:")
    await init_database(engine)

    async with factory() as session:
        product = Product(
            name="Bulk product",
            description="Description",
            price=Decimal("1.00"),
            button_emoji="📦",
            active=True,
        )
        session.add(product)
        await session.commit()
        product_id = product.id

        unique_items = [f"ACCOUNT-{index:04d}" for index in range(1_205)]
        added, duplicates = await add_stock_items(
            session,
            product_id,
            unique_items + unique_items[:11],
        )

        assert added == 1_205
        assert duplicates == 11

    async with factory() as session:
        count = await session.scalar(
            select(func.count(StockItem.id)).where(StockItem.product_id == product_id)
        )
        assert count == 1_205
        added, duplicates = await add_stock_items(session, product_id, unique_items[:7])
        assert added == 0
        assert duplicates == 7

    await engine.dispose()


@pytest.mark.asyncio
async def test_stock_import_accepts_multiline_text_and_text_document() -> None:
    telegram_user = TelegramUser(id=99, is_bot=False, first_name="Admin")
    chat = Chat(id=99, type=ChatType.PRIVATE)
    text_message = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=chat,
        from_user=telegram_user,
        text="one@example.com\nhttps://example.com/key\nCODE-3",
    )

    text_items = await extract_stock_payloads(text_message, SimpleNamespace())  # type: ignore[arg-type]
    assert text_items == ["one@example.com", "https://example.com/key", "CODE-3"]

    raw = b"ACCOUNT-1\nACCOUNT-2\nACCOUNT-3\n"
    document = Document(
        file_id="file-id",
        file_unique_id="unique-id",
        file_name="stock.txt",
        mime_type="text/plain",
        file_size=len(raw),
    )
    document_message = Message(
        message_id=2,
        date=datetime.now(UTC),
        chat=chat,
        from_user=telegram_user,
        document=document,
    )

    class DownloadBot:
        async def download(self, _document: object) -> BytesIO:
            return BytesIO(raw)

    document_items = await extract_stock_payloads(
        document_message,
        DownloadBot(),  # type: ignore[arg-type]
    )
    assert document_items == ["ACCOUNT-1", "ACCOUNT-2", "ACCOUNT-3"]


@pytest.mark.asyncio
async def test_purchase_remains_saved_with_delivered_payload_for_history() -> None:
    engine, factory = create_engine_and_session_factory("sqlite+aiosqlite:///:memory:")
    await init_database(engine)

    async with factory() as session:
        user = User(
            telegram_id=7001,
            first_name="Buyer",
            balance=Decimal("10.00"),
            language="es",
        )
        product = Product(
            name="Correo premium",
            description="Description",
            price=Decimal("2.00"),
            button_emoji="📧",
            active=True,
        )
        session.add_all([user, product])
        await session.commit()
        await add_stock_items(session, product.id, ["client@example.com|secret-password"])
        product_id = product.id

    async with factory() as session:
        result = await purchase_product(session, telegram_id=7001, product_id=product_id)
        row = (
            await session.execute(
                select(Order, StockItem.payload)
                .join(StockItem, StockItem.id == Order.stock_item_id)
                .where(Order.order_code == result.order_code)
            )
        ).one()

        assert row[0].product_name == "Correo premium"
        assert row[1] == "client@example.com|secret-password"

    await engine.dispose()


@pytest.mark.asyncio
async def test_stock_notification_is_sent_to_all_registered_non_banned_users(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, factory = create_engine_and_session_factory("sqlite+aiosqlite:///:memory:")
    await init_database(engine)

    async with factory() as session:
        session.add_all(
            [
                User(telegram_id=801, first_name="A", language="es", balance=Decimal("0")),
                User(telegram_id=802, first_name="B", language="en", balance=Decimal("0")),
                User(
                    telegram_id=803,
                    first_name="Blocked by admin",
                    language="es",
                    balance=Decimal("0"),
                    is_banned=True,
                ),
            ]
        )
        await session.commit()

    class BroadcastBot:
        def __init__(self) -> None:
            self.messages: list[tuple[int, str, object]] = []

        async def send_message(
            self,
            telegram_id: int,
            text: str,
            *,
            reply_markup: object,
        ) -> None:
            self.messages.append((telegram_id, text, reply_markup))

    monkeypatch.setattr("app.services.notifications._SEND_DELAY_SECONDS", 0)
    bot = BroadcastBot()
    result = await broadcast_stock_update(
        bot,  # type: ignore[arg-type]
        factory,
        product_id=3,
        product_name="Gemini",
        price=Decimal("1.00"),
        added=25,
        available=30,
    )

    assert result.attempted == 2
    assert result.sent == 2
    assert result.failed == 0
    assert {message[0] for message in bot.messages} == {801, 802}
    assert "Stock actualizado" in bot.messages[0][1]
    assert any("Stock updated" in message[1] for message in bot.messages)

    await engine.dispose()
