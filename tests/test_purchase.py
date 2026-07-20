from decimal import Decimal

import pytest
from sqlalchemy import select

from app.database import create_engine_and_session_factory, init_database
from app.models import Product, StockItem, User
from app.services.catalog import add_stock_items
from app.services.purchases import OutOfStock, purchase_product


@pytest.mark.asyncio
async def test_purchase_deducts_balance_and_consumes_one_stock_item() -> None:
    engine, factory = create_engine_and_session_factory("sqlite+aiosqlite:///:memory:")
    await init_database(engine)

    async with factory() as session:
        user = User(
            telegram_id=1001,
            first_name="Buyer",
            balance=Decimal("10.00"),
            language="es",
        )
        product = Product(
            name="Test Product",
            description="Description",
            price=Decimal("3.50"),
            button_emoji="🧪",
            active=True,
        )
        session.add_all([user, product])
        await session.commit()
        await add_stock_items(session, product.id, ["CODE-001"])

    async with factory() as session:
        result = await purchase_product(session, telegram_id=1001, product_id=product.id)
    assert result.stock_payload == "CODE-001"
    assert result.new_balance == Decimal("6.50")

    async with factory() as session:
        db_user = await session.scalar(select(User).where(User.telegram_id == 1001))
        stock = await session.scalar(select(StockItem))
        assert db_user is not None and db_user.balance == Decimal("6.50")
        assert stock is not None and stock.status == "sold"

    await engine.dispose()


@pytest.mark.asyncio
async def test_out_of_stock_rolls_back_balance_deduction() -> None:
    engine, factory = create_engine_and_session_factory("sqlite+aiosqlite:///:memory:")
    await init_database(engine)

    async with factory() as session:
        user = User(
            telegram_id=2002,
            first_name="Buyer",
            balance=Decimal("10.00"),
            language="es",
        )
        product = Product(
            name="No Stock",
            description="Description",
            price=Decimal("4.00"),
            button_emoji="🧪",
            active=True,
        )
        session.add_all([user, product])
        await session.commit()

    async with factory() as session:
        with pytest.raises(OutOfStock):
            await purchase_product(session, telegram_id=2002, product_id=product.id)

    async with factory() as session:
        db_user = await session.scalar(select(User).where(User.telegram_id == 2002))
        assert db_user is not None and db_user.balance == Decimal("10.00")

    await engine.dispose()
