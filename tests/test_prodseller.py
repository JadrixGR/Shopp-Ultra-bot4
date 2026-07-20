from __future__ import annotations

from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select

from app.database import create_engine_and_session_factory, init_database
from app.models import Order, Product, ProviderPurchase, StockItem, User
from app.services.external_purchases import (
    ExternalOrderManualReview,
    ExternalProviderBalanceLow,
    purchase_prodseller_product,
)
from app.services.prodseller import ProdSellerClient
from app.services.provider_catalog import sync_prodseller_catalog


def client_with_handler(handler) -> ProdSellerClient:  # type: ignore[no-untyped-def]
    return ProdSellerClient(
        api_key="psk_test_key",
        base_url="https://provider.test/v1",
        allow_insecure_http=False,
        timeout_seconds=5,
        cache_seconds=0,
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.asyncio
async def test_prodseller_client_sends_api_key_and_parses_responses() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-API-Key"] == "psk_test_key"
        if request.url.path.endswith("/products"):
            return httpx.Response(
                200,
                headers={"X-RateLimit-Limit": "300", "X-RateLimit-Remaining": "299"},
                json={
                    "products": [
                        {
                            "id": "abc123",
                            "name": "Digital account",
                            "description": "Instant",
                            "price": 4.99,
                            "delivery": {"type": "instant"},
                            "inStock": True,
                            "sold": 7,
                        }
                    ]
                },
            )
        if request.url.path.endswith("/balance"):
            return httpx.Response(
                200,
                json={
                    "telegramId": 123,
                    "username": "seller",
                    "balance": 25.5,
                    "membership": "gold",
                },
            )
        raise AssertionError(request.url)

    client = client_with_handler(handler)
    products = await client.list_products(force_refresh=True)
    balance = await client.get_balance()

    assert products[0].id == "abc123"
    assert products[0].price == Decimal("4.99")
    assert products[0].in_stock is True
    assert balance.balance == Decimal("25.50")
    assert client.rate_limit.remaining == 299
    await client.close()


@pytest.mark.asyncio
async def test_catalog_sync_imports_products_with_markup_and_preserves_edited_price() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "products": [
                    {
                        "id": "product-1",
                        "name": "Gemini access",
                        "description": "Delivered instantly",
                        "price": 2,
                        "imageUrl": "https://example.test/image.png",
                        "delivery": {"type": "instant"},
                        "inStock": True,
                    }
                ]
            },
        )

    client = client_with_handler(handler)
    engine, factory = create_engine_and_session_factory("sqlite+aiosqlite:///:memory:")
    await init_database(engine)

    async with factory() as session:
        result = await sync_prodseller_catalog(
            session,
            client,
            markup_percent=Decimal("25"),
            update_prices=False,
        )
        imported = await session.scalar(select(Product))
        assert imported is not None
        assert imported.price == Decimal("2.50")
        assert imported.provider_code == "prodseller"
        assert imported.external_product_id == "product-1"
        imported.price = Decimal("9.00")
        await session.commit()

    async with factory() as session:
        result2 = await sync_prodseller_catalog(
            session,
            client,
            markup_percent=Decimal("25"),
            update_prices=False,
        )
        imported = await session.scalar(select(Product))
        assert imported is not None
        assert imported.price == Decimal("9.00")

    assert result.created == 1
    assert result2.updated == 1
    assert calls == 2
    await client.close()
    await engine.dispose()


@pytest.mark.asyncio
async def test_external_purchase_delivers_key_and_saves_normal_history_order() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/products/external-1"):
            return httpx.Response(
                200,
                json={
                    "id": "external-1",
                    "name": "External account",
                    "price": 4,
                    "stock": 3,
                    "delivery": {"type": "instant"},
                },
            )
        if request.method == "POST" and request.url.path.endswith("/orders"):
            assert request.headers["X-API-Key"] == "psk_test_key"
            assert request.content
            return httpx.Response(
                200,
                json={
                    "orderId": "provider-order-1",
                    "status": "delivered",
                    "product": {"id": "external-1", "name": "External account"},
                    "quantity": 1,
                    "amount": 3.8,
                    "discountPercent": 5,
                    "discountAmount": 0.2,
                    "deliveredKey": "mail@example.com:secret",
                },
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    client = client_with_handler(handler)
    engine, factory = create_engine_and_session_factory("sqlite+aiosqlite:///:memory:")
    await init_database(engine)

    async with factory() as session:
        user = User(
            telegram_id=5001,
            first_name="Buyer",
            language="es",
            balance=Decimal("10.00"),
        )
        product = Product(
            name="External account",
            description="API product",
            price=Decimal("5.00"),
            button_emoji="⚡",
            active=True,
            provider_code="prodseller",
            external_product_id="external-1",
            provider_in_stock=True,
        )
        session.add_all([user, product])
        await session.commit()
        product_id = product.id

    result = await purchase_prodseller_product(
        factory,
        client,
        telegram_id=5001,
        product_id=product_id,
        allow_below_cost=False,
        poll_attempts=2,
        poll_delay_seconds=0,
    )

    assert result.stock_payload == "mail@example.com:secret"
    assert result.new_balance == Decimal("5.00")

    async with factory() as session:
        saved_user = await session.scalar(select(User).where(User.telegram_id == 5001))
        saved_order = await session.scalar(select(Order))
        stock = await session.scalar(select(StockItem))
        provider_purchase = await session.scalar(select(ProviderPurchase))
        assert saved_user is not None and saved_user.balance == Decimal("5.00")
        assert saved_order is not None
        assert saved_order.provider_order_id == "provider-order-1"
        assert stock is not None and stock.payload == "mail@example.com:secret"
        assert provider_purchase is not None and provider_purchase.status == "delivered"

    await client.close()
    await engine.dispose()


@pytest.mark.asyncio
async def test_provider_insufficient_balance_refunds_customer() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "id": "external-2",
                    "name": "External account",
                    "price": 2,
                    "stock": 1,
                    "delivery": {"type": "instant"},
                },
            )
        return httpx.Response(402, json={"error": "Solde insuffisant"})

    client = client_with_handler(handler)
    engine, factory = create_engine_and_session_factory("sqlite+aiosqlite:///:memory:")
    await init_database(engine)
    async with factory() as session:
        session.add_all(
            [
                User(
                    telegram_id=6001,
                    first_name="Buyer",
                    language="es",
                    balance=Decimal("10.00"),
                ),
                Product(
                    name="External",
                    description="API",
                    price=Decimal("4.00"),
                    button_emoji="⚡",
                    active=True,
                    provider_code="prodseller",
                    external_product_id="external-2",
                    provider_in_stock=True,
                ),
            ]
        )
        await session.commit()
        product = await session.scalar(select(Product))
        assert product is not None
        product_id = product.id

    with pytest.raises(ExternalProviderBalanceLow):
        await purchase_prodseller_product(
            factory,
            client,
            telegram_id=6001,
            product_id=product_id,
            allow_below_cost=False,
            poll_attempts=1,
            poll_delay_seconds=0,
        )

    async with factory() as session:
        user = await session.scalar(select(User).where(User.telegram_id == 6001))
        purchase = await session.scalar(select(ProviderPurchase))
        assert user is not None and user.balance == Decimal("10.00")
        assert purchase is not None and purchase.status == "refunded"

    await client.close()
    await engine.dispose()


@pytest.mark.asyncio
async def test_ambiguous_post_keeps_reserved_balance_for_manual_review() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "id": "external-3",
                    "name": "External account",
                    "price": 2,
                    "stock": 1,
                    "delivery": {"type": "instant"},
                },
            )
        raise httpx.ReadTimeout("response lost", request=request)

    client = client_with_handler(handler)
    engine, factory = create_engine_and_session_factory("sqlite+aiosqlite:///:memory:")
    await init_database(engine)
    async with factory() as session:
        session.add_all(
            [
                User(
                    telegram_id=7001,
                    first_name="Buyer",
                    language="es",
                    balance=Decimal("10.00"),
                ),
                Product(
                    name="External",
                    description="API",
                    price=Decimal("4.00"),
                    button_emoji="⚡",
                    active=True,
                    provider_code="prodseller",
                    external_product_id="external-3",
                    provider_in_stock=True,
                ),
            ]
        )
        await session.commit()
        product = await session.scalar(select(Product))
        assert product is not None
        product_id = product.id

    with pytest.raises(ExternalOrderManualReview):
        await purchase_prodseller_product(
            factory,
            client,
            telegram_id=7001,
            product_id=product_id,
            allow_below_cost=False,
            poll_attempts=1,
            poll_delay_seconds=0,
        )

    async with factory() as session:
        user = await session.scalar(select(User).where(User.telegram_id == 7001))
        purchase = await session.scalar(select(ProviderPurchase))
        assert user is not None and user.balance == Decimal("6.00")
        assert purchase is not None and purchase.status == "manual_review"

    await client.close()
    await engine.dispose()


@pytest.mark.asyncio
async def test_existing_database_is_migrated_without_losing_user_balance(tmp_path) -> None:
    import sqlite3

    db_path = tmp_path / "legacy.db"
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            telegram_id BIGINT NOT NULL UNIQUE,
            username VARCHAR(64),
            first_name VARCHAR(128) NOT NULL DEFAULT '',
            language VARCHAR(5) NOT NULL DEFAULT 'es',
            balance NUMERIC(18, 2) NOT NULL DEFAULT 0,
            is_banned BOOLEAN NOT NULL DEFAULT 0,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL
        );
        CREATE TABLE products (
            id INTEGER PRIMARY KEY,
            name VARCHAR(180) NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            price NUMERIC(18, 2) NOT NULL,
            button_emoji VARCHAR(32) NOT NULL DEFAULT '🛍️',
            media_type VARCHAR(16),
            media_file_id TEXT,
            active BOOLEAN NOT NULL DEFAULT 1,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL
        );
        INSERT INTO users (
            id, telegram_id, username, first_name, language, balance,
            is_banned, created_at, updated_at
        ) VALUES (
            1, 9001, 'legacy', 'Legacy', 'es', 37.50,
            0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
        );
        INSERT INTO products (
            id, name, description, price, button_emoji, active,
            created_at, updated_at
        ) VALUES (
            1, 'Legacy product', 'kept', 2.00, '📦', 1,
            CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
        );
        """
    )
    connection.commit()
    connection.close()

    engine, factory = create_engine_and_session_factory(f"sqlite+aiosqlite:///{db_path.as_posix()}")
    await init_database(engine)

    async with factory() as session:
        user = await session.scalar(select(User).where(User.telegram_id == 9001))
        product = await session.get(Product, 1)
        assert user is not None and user.balance == Decimal("37.50")
        assert product is not None and product.name == "Legacy product"
        assert product.provider_code is None

    raw = sqlite3.connect(db_path)
    product_columns = {row[1] for row in raw.execute("PRAGMA table_info(products)")}
    order_columns = {row[1] for row in raw.execute("PRAGMA table_info(orders)")}
    raw.close()
    assert {"provider_code", "external_product_id", "provider_cost"}.issubset(product_columns)
    assert {"provider_code", "provider_order_id", "provider_amount"}.issubset(order_columns)

    await engine.dispose()
