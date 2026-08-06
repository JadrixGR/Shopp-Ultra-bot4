from __future__ import annotations

import sqlite3
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.database import create_engine_and_session_factory, init_database
from app.handlers.store import _maximum_purchase_quantity
from app.keyboards import product_keyboard, store_keyboard
from app.models import Order, Product, StockItem, User
from app.services.catalog import add_stock_items, get_product_with_stock
from app.services.purchases import MAX_PURCHASE_QUANTITY, purchase_product


@pytest.mark.asyncio
async def test_infinite_stock_delivers_same_message_without_running_out() -> None:
    engine, factory = create_engine_and_session_factory("sqlite+aiosqlite:///:memory:")
    await init_database(engine)
    delivery_message = "Contactar con el admin @JadrixGR02"

    async with factory() as session:
        user = User(
            telegram_id=7001,
            first_name="Buyer",
            balance=Decimal("20.00"),
            language="es",
        )
        product = Product(
            name="Entrega manual",
            description="Producto con atención del administrador",
            price=Decimal("2.00"),
            button_emoji="📨",
            active=True,
            infinite_stock=True,
            infinite_stock_message=delivery_message,
        )
        session.add_all([user, product])
        await session.commit()
        product_id = product.id

        # Normal stock is retained as a backup and ignored while infinite mode is active.
        await add_stock_items(session, product_id, ["BACKUP-CODE"])
        item = await get_product_with_stock(session, product_id)
        assert item is not None
        assert item.available is True
        assert item.stock_text("es") == "∞"
        assert _maximum_purchase_quantity(item) == MAX_PURCHASE_QUANTITY
        store_markup = store_keyboard("es", [item])
        assert "∞" in store_markup.inline_keyboard[0][0].text

    async with factory() as session:
        first = await purchase_product(
            session,
            telegram_id=7001,
            product_id=product_id,
            quantity=2,
        )
        second = await purchase_product(
            session,
            telegram_id=7001,
            product_id=product_id,
        )

    assert first.quantity == 2
    assert first.stock_payload.count(delivery_message) == 2
    assert second.stock_payload == delivery_message
    assert second.new_balance == Decimal("14.00")

    async with factory() as session:
        item = await get_product_with_stock(session, product_id)
        sold = await session.scalar(
            select(func.count(StockItem.id)).where(StockItem.status == "sold")
        )
        available = await session.scalar(
            select(func.count(StockItem.id)).where(StockItem.status == "available")
        )
        orders = await session.scalar(select(func.count(Order.id)))
        hashes = list(await session.scalars(select(StockItem.payload_hash)))

        assert item is not None and item.stock_text("es") == "∞"
        assert sold == 3
        assert available == 1
        assert orders == 3
        assert len(hashes) == len(set(hashes))

        item.product.infinite_stock = False
        item.product.infinite_stock_message = None
        await session.commit()
        restored = await get_product_with_stock(session, product_id)
        assert restored is not None
        assert restored.stock_text("es") == "1"

    await engine.dispose()


def test_product_keyboard_allows_infinite_product_with_zero_counted_units() -> None:
    markup = product_keyboard(
        "es",
        product_id=9,
        page=0,
        price=Decimal("3.00"),
        stock=0,
        available=True,
    )

    assert markup.inline_keyboard[0][0].callback_data == "buy:9:0"


@pytest.mark.asyncio
async def test_existing_sqlite_database_gets_infinite_stock_columns(tmp_path) -> None:
    database = tmp_path / "legacy.db"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE products (id INTEGER PRIMARY KEY)")
    connection.commit()
    connection.close()

    engine, _factory = create_engine_and_session_factory(
        f"sqlite+aiosqlite:///{database.as_posix()}"
    )
    await init_database(engine)
    await engine.dispose()

    connection = sqlite3.connect(database)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(products)")}
    connection.close()

    assert {"infinite_stock", "infinite_stock_message"}.issubset(columns)
