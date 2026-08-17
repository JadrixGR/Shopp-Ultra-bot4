from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select

from app.config import Settings
from app.database import create_engine_and_session_factory, init_database
from app.models import Order, Product, ProviderPurchase, StockItem, User
from app.services.external_purchases import purchase_provider_product
from app.services.prodseller import (
    ProdSellerAuthenticationError,
    ProdSellerInsufficientBalanceError,
    ProdSellerOutOfStockError,
    ProdSellerRateLimitError,
)
from app.services.provider_catalog import sync_provider_catalog
from app.services.provider_options import product_provider_options
from app.services.provider_registry import build_provider_registry, load_provider_configs
from app.services.ventebot_reseller import VenteBotResellerClient


def ventebot_client(handler) -> VenteBotResellerClient:  # type: ignore[no-untyped-def]
    return VenteBotResellerClient(
        api_key="vbr_test_key",
        base_url="https://ventebot.test/api/swagger/",
        allow_insecure_http=False,
        api_key_header="X-Reseller-Key",
        cache_seconds=0,
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.asyncio
async def test_catalog_uses_reseller_header_and_reads_exact_stock() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/reseller/products"
        assert request.headers["X-Reseller-Key"] == "vbr_test_key"
        return httpx.Response(
            200,
            headers={
                "X-RateLimit-Limit": "60",
                "X-RateLimit-Remaining": "59",
                "X-RateLimit-Reset": "1787000000",
            },
            json={
                "success": True,
                "products": [
                    {
                        "id": "activation-product",
                        "name": "Activation Product",
                        "description": "Delivered after activation",
                        "image_url": "/media/product.png",
                        "price_usd": "2.50",
                        "standard_price_usd": "3.00",
                        "pricing_type": "tiered",
                        "delivery_type": "activation_identifier",
                        "stock": 37,
                        "price_tiers": [{"min_qty": 2, "max_qty": 10, "price_usd": "2.00"}],
                        "api_test": False,
                    }
                ],
            },
        )

    client = ventebot_client(handler)
    product = (await client.list_products(force_refresh=True))[0]

    assert client.base_url == "https://ventebot.test"
    assert product.id == "activation-product"
    assert product.price == Decimal("2.50")
    assert product.stock == 37
    assert product.in_stock is True
    assert product.requires_activation_identifier is True
    assert product.image_url == "https://ventebot.test/media/product.png"
    assert client.rate_limit.limit == 60
    assert client.rate_limit.remaining == 59
    await client.close()


@pytest.mark.asyncio
async def test_account_quote_order_tracking_and_activation_endpoint() -> None:
    requests: list[tuple[str, str, dict[str, object] | None]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        requests.append((request.method, request.url.path, body))
        if request.url.path == "/api/reseller/me":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "user_telegram_id": 123456,
                    "username": "reseller",
                    "first_name": "Seller",
                    "wallet_balance": "45.75",
                    "key_name": "Shop Ultra",
                    "key_prefix": "vbr_test",
                },
            )
        if request.url.path == "/api/reseller/quote":
            assert body == {"product_id": "product-1", "quantity": 3}
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "quote": {
                        "product_id": "product-1",
                        "quantity": 3,
                        "unit_price_usd": "1.50",
                        "amount_usd": "4.50",
                    },
                    "wallet_balance": "45.75",
                },
            )
        if request.method == "POST" and request.url.path == "/api/reseller/orders":
            assert body == {
                "product_id": "product-1",
                "quantity": 1,
                "activation_identifier": "customer@example.com",
                "customer_reference": "API-LOCAL-1",
                "idempotency_key": "API-LOCAL-1",
            }
            return httpx.Response(
                201,
                json={
                    "success": True,
                    "order": {
                        "id": "vente-order-1",
                        "status": "completed",
                        "product_id": "product-1",
                        "product_name": "Product One",
                        "quantity": 1,
                        "amount_usd": "1.50",
                        "delivery_type": "instant",
                        "customer_reference": "API-LOCAL-1",
                        "idempotency_key": "API-LOCAL-1",
                        "created_at": "2026-08-17T12:00:00Z",
                        "items": [
                            {
                                "id": "item-1",
                                "account_data": {
                                    "email": "delivered@example.com",
                                    "password": "pass-123",
                                },
                            }
                        ],
                    },
                },
            )
        if request.method == "GET" and request.url.path == "/api/reseller/orders/vente-order-1":
            return httpx.Response(
                200,
                json={
                    "id": "vente-order-1",
                    "status": "completed",
                    "product_id": "product-1",
                    "product_name": "Product One",
                    "quantity": 1,
                    "amount_usd": "1.50",
                    "items": [{"id": "item-1", "account_data": "license-key-1"}],
                },
            )
        if request.url.path == "/api/reseller/orders/vente-order-1/activation-identifier":
            assert body == {"activation_identifier": "new@example.com"}
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "order": {
                        "id": "vente-order-1",
                        "status": "processing",
                        "product_id": "product-1",
                        "product_name": "Product One",
                        "quantity": 1,
                        "amount_usd": "1.50",
                        "items": [],
                    },
                },
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    client = ventebot_client(handler)
    balance = await client.get_balance()
    quote = await client.quote_order("product-1", quantity=3)
    created = await client.create_order(
        "product-1",
        purchase_options={
            "activation_identifier": "customer@example.com",
            "_idempotency_key": "API-LOCAL-1",
            "_customer_reference": "API-LOCAL-1",
        },
    )
    tracked = await client.get_order("vente-order-1")
    activated = await client.submit_activation_identifier("vente-order-1", "new@example.com")

    assert balance.telegram_id == 123456
    assert balance.balance == Decimal("45.75")
    assert balance.membership == "Shop Ultra"
    assert quote == Decimal("4.50")
    assert created.delivered is True
    assert "delivered@example.com" in created.delivery_payload
    assert "pass-123" in created.delivery_payload
    assert tracked.delivery_payload == "license-key-1"
    assert activated.status == "processing"
    assert [path for _method, path, _body in requests] == [
        "/api/reseller/me",
        "/api/reseller/quote",
        "/api/reseller/orders",
        "/api/reseller/orders/vente-order-1",
        "/api/reseller/orders/vente-order-1/activation-identifier",
    ]
    await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "payload", "error_type"),
    [
        (
            401,
            {"success": False, "code": "invalid_key", "message": "Invalid key"},
            ProdSellerAuthenticationError,
        ),
        (
            402,
            {"success": False, "code": "insufficient_balance", "message": "Insufficient balance"},
            ProdSellerInsufficientBalanceError,
        ),
        (
            409,
            {"success": False, "code": "out_of_stock", "message": "Out of stock"},
            ProdSellerOutOfStockError,
        ),
        (
            429,
            {"success": False, "code": "rate_limited", "message": "Too many requests"},
            ProdSellerRateLimitError,
        ),
    ],
)
async def test_documented_errors_are_mapped(
    status: int, payload: dict[str, object], error_type: type[Exception]
) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)

    client = ventebot_client(handler)
    with pytest.raises(error_type):
        await client.get_balance()
    await client.close()


@pytest.mark.asyncio
async def test_render_environment_builds_and_persists_ventebot_provider(tmp_path) -> None:
    providers_file = tmp_path / "providers.json"
    settings = Settings(
        BOT_TOKEN="1234567890:abcdefghijklmnopqrstuvwxyzABCDE",
        ADMIN_IDS="123456789",
        API_PROVIDERS_FILE=str(providers_file),
        VENTEBOT_ENABLED=True,
        VENTEBOT_API_KEY="vbr_test_render_key",
        VENTEBOT_MARKUP_PERCENT="35",
    )

    configs = load_provider_configs(settings)
    registry = build_provider_registry(settings)
    runtime = registry.get("ventebot")
    saved = json.loads(providers_file.read_text(encoding="utf-8"))

    assert len(configs) == 1
    assert configs[0].adapter == "ventebot_reseller_v1"
    assert configs[0].api_key_header == "X-Reseller-Key"
    assert configs[0].markup_percent == Decimal("35.00")
    assert runtime is not None
    assert isinstance(runtime.client, VenteBotResellerClient)
    assert saved["providers"][0]["code"] == "ventebot"
    assert saved["providers"][0]["api_key"] == "vbr_test_render_key"
    await registry.close()


@pytest.mark.asyncio
async def test_tier_quote_idempotent_purchase_and_delivery_are_saved() -> None:
    order_request: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/reseller/products":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "products": [
                        {
                            "id": "tiered-product",
                            "name": "Tiered activation",
                            "description": "Activation product",
                            "price_usd": "2.00",
                            "pricing_type": "tiered",
                            "delivery_type": "activation_identifier",
                            "stock": 10,
                            "price_tiers": [{"min_qty": 2, "max_qty": 10, "price_usd": "1.50"}],
                        }
                    ],
                },
            )
        if request.method == "POST" and request.url.path == "/api/reseller/quote":
            assert json.loads(request.content) == {
                "product_id": "tiered-product",
                "quantity": 2,
            }
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "quote": {
                        "product_id": "tiered-product",
                        "quantity": 2,
                        "unit_price_usd": "1.50",
                        "amount_usd": "3.00",
                    },
                    "wallet_balance": "100.00",
                },
            )
        if request.method == "POST" and request.url.path == "/api/reseller/orders":
            order_request.update(json.loads(request.content))
            assert str(order_request["idempotency_key"]).startswith("API-")
            assert order_request["customer_reference"] == order_request["idempotency_key"]
            return httpx.Response(
                201,
                json={
                    "success": True,
                    "order": {
                        "id": "vente-tier-order",
                        "status": "completed",
                        "product_id": "tiered-product",
                        "product_name": "Tiered activation",
                        "quantity": 2,
                        "amount_usd": "3.00",
                        "delivery_type": "activation_identifier",
                        "customer_reference": order_request["customer_reference"],
                        "idempotency_key": order_request["idempotency_key"],
                        "activation_identifier": order_request["activation_identifier"],
                        "created_at": "2026-08-17T12:00:00Z",
                        "items": [
                            {"id": "one", "account_data": "first-account"},
                            {
                                "id": "two",
                                "account_data": {
                                    "email": "second@example.com",
                                    "password": "second-pass",
                                },
                            },
                        ],
                    },
                },
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    client = ventebot_client(handler)
    engine, factory = create_engine_and_session_factory("sqlite+aiosqlite:///:memory:")
    await init_database(engine)

    async with factory() as session:
        await sync_provider_catalog(
            session,
            client,
            provider_code="ventebot",
            markup_percent=Decimal("50"),
            new_products_active=True,
        )
        product = await session.scalar(select(Product))
        assert product is not None
        assert product.price == Decimal("3.00")
        assert product.provider_stock == 10
        assert product_provider_options(product).requires_activation_identifier is True
        session.add(User(telegram_id=700100, first_name="Buyer", balance=Decimal("10.00")))
        await session.commit()
        product_id = product.id

    result = await purchase_provider_product(
        factory,
        client,
        provider_code="ventebot",
        telegram_id=700100,
        product_id=product_id,
        allow_below_cost=False,
        poll_attempts=2,
        poll_delay_seconds=0,
        purchase_options={"activation_identifier": "buyer@example.com"},
        requested_quantity=2,
    )

    assert result.price == Decimal("6.00")
    assert result.new_balance == Decimal("4.00")
    assert result.quantity == 2
    assert "first-account" in result.stock_payload
    assert "second@example.com" in result.stock_payload
    assert order_request == {
        "product_id": "tiered-product",
        "quantity": 2,
        "activation_identifier": "buyer@example.com",
        "customer_reference": order_request["idempotency_key"],
        "idempotency_key": order_request["idempotency_key"],
    }

    async with factory() as session:
        user = await session.scalar(select(User).where(User.telegram_id == 700100))
        product = await session.get(Product, product_id)
        purchase = await session.scalar(select(ProviderPurchase))
        order = await session.scalar(select(Order))
        stock_item = await session.scalar(select(StockItem))
        assert user is not None and user.balance == Decimal("4.00")
        assert product is not None and product.provider_stock == 8
        assert purchase is not None
        assert purchase.status == "delivered"
        assert purchase.expected_provider_cost == Decimal("3.00")
        assert purchase.actual_provider_amount == Decimal("3.00")
        assert order is not None and order.provider_order_id == "vente-tier-order"
        assert stock_item is not None and "second-pass" in stock_item.payload

    await client.close()
    await engine.dispose()
