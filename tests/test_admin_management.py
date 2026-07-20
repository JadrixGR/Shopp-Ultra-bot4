from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.database import create_engine_and_session_factory, init_database
from app.models import BalanceAdjustment, Order, Product, Refund, StockItem, User
from app.services.admin_finance import (
    BalanceWouldBeNegative,
    NothingToRefund,
    adjust_user_balance,
    build_refund_preview,
    calculate_prorated_target,
    refund_order,
)
from app.services.notifications import ProductNotice, broadcast_catalog_update
from app.services.settings import get_provider_auto_publish, set_provider_auto_publish


def test_prorated_refund_calculation() -> None:
    assert calculate_prorated_target(
        Decimal("20.00"),
        total_days=30,
        used_days=15,
    ) == Decimal("10.00")

    preview = build_refund_preview(
        price=Decimal("20.00"),
        already_refunded=Decimal("2.00"),
        refund_type="prorated",
        total_days=30,
        used_days=15,
    )
    assert preview.target_total_refund == Decimal("10.00")
    assert preview.amount_to_credit == Decimal("8.00")
    assert preview.remaining_days == 15


@pytest.mark.asyncio
async def test_refund_and_manual_balance_adjustments_are_audited() -> None:
    engine, factory = create_engine_and_session_factory("sqlite+aiosqlite:///:memory:")
    await init_database(engine)

    async with factory() as session:
        user = User(
            telegram_id=71001,
            first_name="Buyer",
            balance=Decimal("5.00"),
            language="es",
        )
        product = Product(
            name="Servicio 30 días",
            description="",
            price=Decimal("20.00"),
            button_emoji="📦",
            active=True,
            service_days=30,
        )
        session.add_all([user, product])
        await session.flush()
        stock = StockItem(
            product_id=product.id,
            payload="buyer@example.com:secret",
            payload_hash="a" * 64,
            status="sold",
            sold_to_user_id=user.id,
        )
        session.add(stock)
        await session.flush()
        order = Order(
            order_code="ORD-REFUND-1",
            user_id=user.id,
            product_id=product.id,
            stock_item_id=stock.id,
            product_name=product.name,
            price=Decimal("20.00"),
            status="completed",
        )
        session.add(order)
        await session.commit()
        order_id = order.id

    async with factory() as session:
        result = await refund_order(
            session,
            order_id=order_id,
            admin_telegram_id=999,
            refund_type="prorated",
            total_days=30,
            used_days=15,
            reason="Cliente usó 15 de 30 días",
        )
        assert result.amount == Decimal("10.00")
        assert result.new_balance == Decimal("15.00")
        assert result.refund_status == "partial"

    async with factory() as session:
        saved_user = await session.scalar(select(User).where(User.telegram_id == 71001))
        saved_order = await session.get(Order, order_id)
        refund = await session.scalar(select(Refund).where(Refund.order_id == order_id))
        adjustment = await session.scalar(
            select(BalanceAdjustment).where(BalanceAdjustment.reference_type == "refund")
        )
        assert saved_user is not None and saved_user.balance == Decimal("15.00")
        assert saved_order is not None and saved_order.refunded_amount == Decimal("10.00")
        assert saved_order.refund_status == "partial"
        assert refund is not None and refund.amount == Decimal("10.00")
        assert adjustment is not None and adjustment.amount == Decimal("10.00")

    async with factory() as session:
        with pytest.raises(NothingToRefund):
            await refund_order(
                session,
                order_id=order_id,
                admin_telegram_id=999,
                refund_type="prorated",
                total_days=30,
                used_days=15,
                reason="Repeated",
            )

    async with factory() as session:
        adjustment_result = await adjust_user_balance(
            session,
            telegram_id=71001,
            amount=Decimal("5.00"),
            admin_telegram_id=999,
            reason="Compensación adicional",
        )
        assert adjustment_result.balance_before == Decimal("15.00")
        assert adjustment_result.balance_after == Decimal("20.00")

    async with factory() as session:
        with pytest.raises(BalanceWouldBeNegative):
            await adjust_user_balance(
                session,
                telegram_id=71001,
                amount=Decimal("-100.00"),
                admin_telegram_id=999,
                reason="Invalid debit",
            )
        count = int(await session.scalar(select(func.count(BalanceAdjustment.id))) or 0)
        assert count == 2

    await engine.dispose()


@pytest.mark.asyncio
async def test_provider_auto_publish_setting_is_persistent() -> None:
    engine, factory = create_engine_and_session_factory("sqlite+aiosqlite:///:memory:")
    await init_database(engine)

    async with factory() as session:
        assert await get_provider_auto_publish(session, "provider_one") is False
        await set_provider_auto_publish(session, "provider_one", True)

    async with factory() as session:
        assert await get_provider_auto_publish(session, "provider_one") is True
        await set_provider_auto_publish(session, "provider_one", False)

    async with factory() as session:
        assert await get_provider_auto_publish(session, "provider_one") is False

    await engine.dispose()


@pytest.mark.asyncio
async def test_catalog_notification_combines_multiple_products(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, factory = create_engine_and_session_factory("sqlite+aiosqlite:///:memory:")
    await init_database(engine)
    async with factory() as session:
        session.add_all(
            [
                User(
                    telegram_id=92001,
                    first_name="A",
                    language="es",
                    balance=Decimal("0.00"),
                ),
                User(
                    telegram_id=92002,
                    first_name="B",
                    language="en",
                    balance=Decimal("0.00"),
                ),
            ]
        )
        await session.commit()

    class Bot:
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
    bot = Bot()
    result = await broadcast_catalog_update(
        bot,  # type: ignore[arg-type]
        factory,
        products=[
            ProductNotice(1, "Product A", Decimal("1.00")),
            ProductNotice(2, "Product B", Decimal("2.00")),
        ],
    )

    assert result.sent == 2
    assert any("Nuevos productos" in text for _user, text, _markup in bot.messages)
    assert any("New products" in text for _user, text, _markup in bot.messages)
    assert all(
        markup.inline_keyboard[0][0].callback_data == "shop:0" for _, _, markup in bot.messages
    )

    await engine.dispose()
