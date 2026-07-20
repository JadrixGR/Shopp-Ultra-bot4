from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from aiogram.types import User as TelegramUser
from sqlalchemy import func, select

from app.database import create_engine_and_session_factory, init_database
from app.handlers import common
from app.handlers.store import _product_view_text
from app.keyboards import quantity_selector_keyboard, settings_keyboard
from app.models import Deposit, Order, Product, StockItem, User
from app.services.catalog import add_stock_items, split_stock_payloads
from app.services.purchases import OutOfStock, purchase_product


def test_multiline_stock_uses_double_hyphen_as_unit_delimiter() -> None:
    raw = (
        "correo1@example.com\n"
        "clave-1\n"
        "nota de entrega\n"
        "--\n"
        "correo2@example.com\n"
        "clave-2\n"
        "--\n"
        "https://example.com/licencia\n"
        "código final"
    )

    assert split_stock_payloads(raw) == [
        "correo1@example.com\nclave-1\nnota de entrega",
        "correo2@example.com\nclave-2",
        "https://example.com/licencia\ncódigo final",
    ]


def test_stock_import_keeps_legacy_one_item_per_line_without_delimiter() -> None:
    assert split_stock_payloads("A\nB\n\nC") == ["A", "B", "C"]


def test_quantity_selector_has_only_requested_purchase_actions() -> None:
    markup = quantity_selector_keyboard(
        "es",
        product_id=8,
        page=0,
        quantity=3,
        max_quantity=25,
    )
    callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]
    labels = [button.text for row in markup.inline_keyboard for button in row]

    assert "buyexecute:8:3:0" in callbacks
    assert "buyqtycustom:8:0" in callbacks
    assert any("COMPRAR x3" in label for label in labels)
    assert not any("Copiar enlace" in label or "Ver nota" in label for label in labels)


def test_product_view_renders_optional_instructions_as_separate_block() -> None:
    product = Product(
        name="ChatGPT Plus 1M",
        description="Cuenta de un mes con garantía completa.",
        instructions="Abre el enlace y sigue los pasos de activación.",
        price=Decimal("9.90"),
        button_emoji="📦",
        active=True,
    )

    text = _product_view_text(
        language="es",
        product=product,
        stock_text="21",
        name_limit=300,
        description_limit=1800,
        instructions_limit=1000,
    )

    assert "Productos disponibles" in text
    assert "Producto oficial de la tienda" in text
    assert "Entrega automática instantánea" in text
    assert "<b>Descripción</b>" in text
    assert "<b>Instrucciones</b>" in text
    assert "Stock disponible: <b>21</b>" in text


@pytest.mark.asyncio
async def test_local_bulk_purchase_deducts_total_and_delivers_requested_units() -> None:
    engine, factory = create_engine_and_session_factory("sqlite+aiosqlite:///:memory:")
    await init_database(engine)

    async with factory() as session:
        user = User(
            telegram_id=4001,
            first_name="Buyer",
            balance=Decimal("20.00"),
            language="es",
        )
        product = Product(
            name="Licencia",
            description="Producto",
            price=Decimal("2.50"),
            button_emoji="📦",
            active=True,
        )
        session.add_all([user, product])
        await session.commit()
        product_id = product.id
        await add_stock_items(session, product_id, ["KEY-1", "KEY-2", "KEY-3"])

    async with factory() as session:
        result = await purchase_product(
            session,
            telegram_id=4001,
            product_id=product_id,
            quantity=2,
        )

    assert result.quantity == 2
    assert result.price == Decimal("5.00")
    assert result.new_balance == Decimal("15.00")
    assert "KEY-1" in result.stock_payload and "KEY-2" in result.stock_payload
    assert len(result.order_codes) == 2

    async with factory() as session:
        user = await session.scalar(select(User).where(User.telegram_id == 4001))
        sold = await session.scalar(
            select(func.count(StockItem.id)).where(StockItem.status == "sold")
        )
        orders = await session.scalar(select(func.count(Order.id)))
        assert user is not None and user.balance == Decimal("15.00")
        assert sold == 2
        assert orders == 2

    await engine.dispose()


@pytest.mark.asyncio
async def test_local_bulk_purchase_rolls_back_when_requested_stock_is_missing() -> None:
    engine, factory = create_engine_and_session_factory("sqlite+aiosqlite:///:memory:")
    await init_database(engine)

    async with factory() as session:
        user = User(
            telegram_id=4002,
            first_name="Buyer",
            balance=Decimal("20.00"),
            language="es",
        )
        product = Product(
            name="Licencia",
            description="Producto",
            price=Decimal("2.50"),
            button_emoji="📦",
            active=True,
        )
        session.add_all([user, product])
        await session.commit()
        product_id = product.id
        await add_stock_items(session, product_id, ["ONLY-ONE"])

    async with factory() as session:
        with pytest.raises(OutOfStock):
            await purchase_product(
                session,
                telegram_id=4002,
                product_id=product_id,
                quantity=2,
            )

    async with factory() as session:
        user = await session.scalar(select(User).where(User.telegram_id == 4002))
        stock = await session.scalar(select(StockItem))
        assert user is not None and user.balance == Decimal("20.00")
        assert stock is not None and stock.status == "available"

    await engine.dispose()


@pytest.mark.asyncio
async def test_settings_show_statistics_and_activity_without_language_button(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, factory = create_engine_and_session_factory("sqlite+aiosqlite:///:memory:")
    await init_database(engine)
    now = datetime.now(UTC)

    async with factory() as session:
        user = User(
            telegram_id=5001,
            first_name="Buyer",
            balance=Decimal("7.00"),
            language="es",
        )
        product = Product(
            name="Producto",
            description="Descripción",
            price=Decimal("3.00"),
            button_emoji="📦",
            active=True,
        )
        session.add_all([user, product])
        await session.commit()
        stock = StockItem(
            product_id=product.id,
            payload="entrega",
            payload_hash="a" * 64,
            status="sold",
            sold_to_user_id=user.id,
            sold_at=now,
        )
        session.add(stock)
        await session.flush()
        session.add(
            Order(
                order_code="ORD-STATS",
                user_id=user.id,
                product_id=product.id,
                stock_item_id=stock.id,
                product_name=product.name,
                price=Decimal("3.00"),
                quantity=2,
                status="completed",
                created_at=now - timedelta(days=5),
            )
        )
        session.add(
            Deposit(
                user_id=user.id,
                requested_amount=Decimal("10.00"),
                credited_amount=Decimal("10.50"),
                bonus_amount=Decimal("0.50"),
                status="credited",
                created_at=now - timedelta(days=2),
            )
        )
        await session.commit()

    captured: list[tuple[str, object]] = []

    async def fake_answer_or_replace(_target: object, text: str, markup: object) -> None:
        captured.append((text, markup))

    monkeypatch.setattr(common, "answer_or_replace", fake_answer_or_replace)
    target = SimpleNamespace(from_user=TelegramUser(id=5001, is_bot=False, first_name="Buyer"))
    ctx = SimpleNamespace(session_factory=factory)

    await common._show_settings(target, ctx)  # type: ignore[arg-type]
    stats_text, stats_markup = captured[-1]
    assert "Tus estadísticas" in stats_text
    assert "Ítems comprados: <b>2</b>" in stats_text
    assert "Total gastado: <b>3.00 USDT</b>" in stats_text
    assert "Recargas: <b>10.50 USDT</b>" in stats_text
    assert stats_markup.inline_keyboard[0][0].callback_data == "settings:activity"
    assert not any(
        button.callback_data == "language" for row in stats_markup.inline_keyboard for button in row
    )

    await common._show_settings_activity(target, ctx)  # type: ignore[arg-type]
    activity_text, _activity_markup = captured[-1]
    assert "Recargas de pago" in activity_text
    assert "Método: Binance Pay" in activity_text
    assert "Compras pagadas" in activity_text
    assert "Método: Balance de wallet" in activity_text

    assert settings_keyboard("es").inline_keyboard[0][0].callback_data == "settings:activity"
    await engine.dispose()


@pytest.mark.asyncio
async def test_external_purchase_forwards_selected_quantity_and_charges_total() -> None:
    import httpx

    from app.models import ProviderPurchase
    from app.services.external_purchases import purchase_provider_product
    from app.services.prodseller import ProdSellerClient

    requests_seen: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/products/ext-bulk"):
            return httpx.Response(
                200,
                json={
                    "id": "ext-bulk",
                    "name": "External bulk",
                    "price": 1,
                    "stock": 12,
                    "delivery": {"type": "instant"},
                },
            )
        if request.method == "POST" and request.url.path.endswith("/orders"):
            payload = __import__("json").loads(request.content)
            requests_seen.append(payload)
            return httpx.Response(
                200,
                json={
                    "orderId": "EXT-BULK-ORDER",
                    "status": "delivered",
                    "product": {"id": "ext-bulk", "name": "External bulk"},
                    "quantity": payload["quantity"],
                    "amount": payload["quantity"],
                    "deliveredKeys": [f"KEY-{index}" for index in range(payload["quantity"])],
                },
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    client = ProdSellerClient(
        api_key="secret-key",
        base_url="https://provider.test/v1",
        allow_insecure_http=False,
        provider_name="Provider",
        cache_seconds=0,
        transport=httpx.MockTransport(handler),
    )
    engine, factory = create_engine_and_session_factory("sqlite+aiosqlite:///:memory:")
    await init_database(engine)

    async with factory() as session:
        user = User(
            telegram_id=6001,
            first_name="Buyer",
            balance=Decimal("20.00"),
            language="es",
        )
        product = Product(
            name="External bulk",
            description="API",
            price=Decimal("2.00"),
            button_emoji="⚡",
            active=True,
            provider_code="provider",
            external_product_id="ext-bulk",
            provider_in_stock=True,
            provider_stock=12,
        )
        session.add_all([user, product])
        await session.commit()
        product_id = product.id

    result = await purchase_provider_product(
        factory,
        client,
        provider_code="provider",
        telegram_id=6001,
        product_id=product_id,
        allow_below_cost=False,
        poll_attempts=1,
        poll_delay_seconds=0,
        requested_quantity=3,
    )

    assert requests_seen == [{"productId": "ext-bulk", "quantity": 3}]
    assert result.quantity == 3
    assert result.price == Decimal("6.00")
    assert result.new_balance == Decimal("14.00")
    assert all(f"KEY-{index}" in result.stock_payload for index in range(3))

    async with factory() as session:
        purchase = await session.scalar(select(ProviderPurchase))
        order = await session.scalar(select(Order))
        assert purchase is not None and purchase.quantity == 3
        assert order is not None and order.quantity == 3

    await client.close()
    await engine.dispose()
