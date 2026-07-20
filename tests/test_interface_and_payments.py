from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from aiogram.enums import ChatType, MessageEntityType, StickerType
from aiogram.types import Chat, Message, MessageEntity, Sticker

from app.database import create_engine_and_session_factory, init_database
from app.keyboards import main_menu, store_keyboard
from app.models import Product, User
from app.product_icons import extract_product_icon, pack_product_emoji, product_emoji_parts
from app.services.binance import (
    BinancePayHistoryClient,
    canonical_transaction_reference,
    extract_transaction_reference,
    transaction_reference_aliases,
)
from app.services.catalog import ProductWithStock, list_active_products
from app.services.deposits import (
    DuplicateTransaction,
    create_pending_deposit,
    credit_deposit,
)


def test_main_menu_keeps_requested_layout_and_colors() -> None:
    markup = main_menu("es", is_admin=True)
    rows = markup.inline_keyboard

    assert [len(row) for row in rows] == [1, 2, 2, 1, 1]
    assert [button.callback_data for button in rows[0]] == ["shop:0"]
    assert [button.style for button in rows[0]] == ["success"]
    assert [button.callback_data for button in rows[1]] == ["wallet", "settings"]
    assert [button.style for button in rows[1]] == ["primary", "primary"]
    assert [button.callback_data for button in rows[2]] == ["support", "history"]
    assert [button.style for button in rows[2]] == ["danger", "primary"]
    assert [button.callback_data for button in rows[3]] == ["language"]
    assert [button.style for button in rows[3]] == ["success"]
    assert [button.callback_data for button in rows[4]] == ["admin:home"]
    assert [button.style for button in rows[4]] == ["primary"]


def test_product_button_uses_custom_emoji_icon() -> None:
    packed = pack_product_emoji("5368324170671202286", "1️⃣")
    product = Product(
        id=7,
        name="Gemini AI Pro 18m",
        description="",
        price=Decimal("1.00"),
        button_emoji=packed,
        active=True,
    )
    markup = store_keyboard(
        "es",
        [ProductWithStock(product=product, stock=12)],
        page=0,
        total=1,
    )
    product_button = markup.inline_keyboard[0][0]

    assert product_button.icon_custom_emoji_id == "5368324170671202286"
    assert product_button.text.startswith("Gemini AI Pro 18m")
    assert not product_button.text.startswith("1️⃣")
    assert product_emoji_parts(packed) == ("1️⃣", "5368324170671202286")


def test_store_keyboard_shows_every_product_without_page_controls() -> None:
    products = [
        ProductWithStock(
            product=Product(
                id=index,
                name=f"Producto {index}",
                description="",
                price=Decimal("1.00"),
                button_emoji="📦",
                active=True,
            ),
            stock=1,
        )
        for index in range(1, 26)
    ]

    markup = store_keyboard(
        "es",
        products,
        page=0,
        total=len(products),
        page_size=8,
    )

    assert len(markup.inline_keyboard) == 26
    product_buttons = [row[0] for row in markup.inline_keyboard[:-1]]
    assert [item.callback_data for item in product_buttons] == [
        f"product:{index}:0" for index in range(1, 26)
    ]
    assert all(item.callback_data != "noop" for item in product_buttons)
    assert markup.inline_keyboard[-1][0].callback_data == "menu"


@pytest.mark.asyncio
async def test_full_catalog_query_returns_more_than_twenty_products() -> None:
    engine, factory = create_engine_and_session_factory("sqlite+aiosqlite:///:memory:")
    await init_database(engine)

    async with factory() as session:
        session.add_all(
            [
                Product(
                    name=f"Producto {index}",
                    description="",
                    price=Decimal("1.00"),
                    button_emoji="📦",
                    active=True,
                )
                for index in range(1, 26)
            ]
            + [
                Product(
                    name="Oculto",
                    description="",
                    price=Decimal("1.00"),
                    button_emoji="📦",
                    active=False,
                )
            ]
        )
        await session.commit()

    async with factory() as session:
        rows, total = await list_active_products(session, page=99, page_size=None)

    assert total == 25
    assert len(rows) == 25
    assert all(row.product.active for row in rows)

    await engine.dispose()


def test_custom_emoji_entity_is_extracted_for_product_button() -> None:
    message = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=1, type=ChatType.PRIVATE),
        text="1️⃣",
        entities=[
            MessageEntity(
                type=MessageEntityType.CUSTOM_EMOJI,
                offset=0,
                length=3,
                custom_emoji_id="5368324170671202286",
            )
        ],
    )

    selection = extract_product_icon(message)

    assert selection.custom_emoji_id == "5368324170671202286"
    assert selection.fallback == "1️⃣"
    assert product_emoji_parts(selection.value) == ("1️⃣", "5368324170671202286")


def test_animated_sticker_is_stored_as_product_media() -> None:
    sticker = Sticker(
        file_id="sticker-file-id",
        file_unique_id="sticker-unique-id",
        type=StickerType.REGULAR,
        width=512,
        height=512,
        is_animated=True,
        is_video=False,
        emoji="🛍️",
    )
    message = Message(
        message_id=2,
        date=datetime.now(UTC),
        chat=Chat(id=1, type=ChatType.PRIVATE),
        sticker=sticker,
    )

    selection = extract_product_icon(message)

    assert selection.custom_emoji_id is None
    assert selection.media_type == "sticker"
    assert selection.media_file_id == "sticker-file-id"
    assert selection.fallback == "🛍️"


def test_binance_numeric_order_id_aliases() -> None:
    numeric = "442692493004005376"
    full = f"M_P_{numeric}"

    assert extract_transaction_reference(f"Order ID: {numeric}") == numeric
    assert transaction_reference_aliases(numeric) == frozenset({numeric, full})
    assert transaction_reference_aliases(full) == frozenset({numeric, full})
    assert canonical_transaction_reference(numeric) == full


@pytest.mark.asyncio
async def test_numeric_order_id_matches_binance_prefixed_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    numeric = "442692493004005376"
    transaction = {
        "transactionId": f"M_P_{numeric}",
        "transactionTime": int(now.timestamp() * 1000),
        "amount": "5.00",
        "currency": "USDT",
        "receiverInfo": {"binanceId": "99887766"},
    }
    client = BinancePayHistoryClient(api_key="key", api_secret="secret")

    async def fake_history_for_deposit(*, not_before: datetime, force_refresh: bool):
        del not_before, force_refresh
        return [transaction], 1_700_000_000_000, 1_800_000_000_000

    monkeypatch.setattr(client, "_history_for_deposit", fake_history_for_deposit)
    result = await client.verify_received_transaction(
        transaction_id=numeric,
        expected_pay_id="99887766",
        expected_amount=Decimal("5.00"),
        not_before=now - timedelta(minutes=1),
    )

    assert result.transaction_id == f"M_P_{numeric}"
    await client.close()


@pytest.mark.asyncio
async def test_numeric_and_prefixed_ids_cannot_be_credited_twice() -> None:
    engine, factory = create_engine_and_session_factory("sqlite+aiosqlite:///:memory:")
    await init_database(engine)
    numeric = "442692493004005376"

    async with factory() as session:
        first_user = User(
            telegram_id=3001,
            first_name="First",
            balance=Decimal("0.00"),
            language="es",
        )
        second_user = User(
            telegram_id=3002,
            first_name="Second",
            balance=Decimal("0.00"),
            language="es",
        )
        session.add_all([first_user, second_user])
        await session.commit()
        first = await create_pending_deposit(session, user_id=first_user.id, amount=Decimal("5.00"))
        second = await create_pending_deposit(
            session, user_id=second_user.id, amount=Decimal("5.00")
        )

    async with factory() as session:
        await credit_deposit(
            session,
            deposit_id=first.id,
            transaction_id=f"M_P_{numeric}",
            raw_payload="{}",
            bonus_tiers="",
        )

    async with factory() as session:
        with pytest.raises(DuplicateTransaction):
            await credit_deposit(
                session,
                deposit_id=second.id,
                transaction_id=numeric,
                raw_payload="{}",
                bonus_tiers="",
            )

    await engine.dispose()
