from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select

from app.config import Settings
from app.database import create_engine_and_session_factory, init_database
from app.models import Order, Product, ProviderPurchase, StockItem, User
from app.services.canboso_buyer import CanbosoBuyerClient
from app.services.external_purchases import purchase_provider_product
from app.services.prodseller import (
    ProdSellerAuthenticationError,
    ProdSellerInsufficientBalanceError,
    ProdSellerOutOfStockError,
)
from app.services.provider_catalog import sync_provider_catalog
from app.services.provider_options import product_provider_options
from app.services.provider_registry import build_provider_registry


def canboso_client(handler) -> CanbosoBuyerClient:  # type: ignore[no-untyped-def]
    return CanbosoBuyerClient(
        api_key="tgb_test_key",
        base_url="https://canboso.test",
        allow_insecure_http=False,
        cache_seconds=0,
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.asyncio
async def test_canboso_lists_products_with_query_key_and_parses_special_requirements() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/telegram-buyer/products"
        assert request.url.params["key"] == "tgb_test_key"
        return httpx.Response(
            200,
            json={
                "success": True,
                "walletCurrency": "VND",
                "products": [
                    {
                        "_id": "normal-1",
                        "product_name": "Producto normal",
                        "description": "Entrega instantánea",
                        "usdPricing": 1.85,
                        "stats": {"total": 100, "sold": 40, "available": 60},
                    },
                    {
                        "_id": "slot_chatgpt_business",
                        "product_name": "ChatGPT Business Slot",
                        "usdPricing": 2.5,
                        "isSlotProduct": True,
                        "slotDurations": [1, 3, 6, 12],
                        "requiresCustomerEmail": True,
                        "requiresSlotMonths": True,
                        "quantityFixed": 1,
                        "slotPricingMode": "per_month",
                        "stats": {"available": 12},
                    },
                ],
            },
        )

    client = canboso_client(handler)
    products = await client.list_products(force_refresh=True)

    assert len(products) == 2
    assert products[0].id == "normal-1"
    assert products[0].price == Decimal("1.85")
    assert products[0].stock == 60
    slot = products[1]
    assert slot.requires_customer_email is True
    assert slot.requires_slot_months is True
    assert slot.slot_durations == (1, 3, 6, 12)
    assert slot.slot_pricing_mode == "per_month"
    assert slot.stock == 12

    await client.close()


@pytest.mark.asyncio
async def test_canboso_balance_preserves_native_text_and_usdt_equivalent() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/telegram-buyer/balance"
        assert request.url.params["key"] == "tgb_test_key"
        return httpx.Response(
            200,
            json={
                "success": True,
                "botSource": "primary",
                "walletCurrency": "VND",
                "requester": {"chatId": 1336962312, "name": "buyer"},
                "balance": 250000,
                "balanceVnd": 250000,
                "balanceText": "250.000 ₫",
                "usdtBalance": 10.25,
            },
        )

    client = canboso_client(handler)
    balance = await client.get_balance()

    assert balance.telegram_id == 1336962312
    assert balance.username == "buyer"
    assert balance.currency == "VND"
    assert balance.balance_text == "250.000 ₫"
    assert balance.balance == Decimal("10.25")
    assert balance.membership == "primary · VND"

    await client.close()


@pytest.mark.asyncio
async def test_canboso_purchase_sends_key_and_special_fields_and_parses_delivery() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/telegram-buyer/purchase"
        payload = json.loads(request.content)
        assert payload == {
            "key": "tgb_test_key",
            "product_id": "slot_chatgpt_business",
            "quantity": 1,
            "customer_email": "customer@example.com",
            "slot_months": 3,
        }
        return httpx.Response(
            200,
            json={
                "success": True,
                "orderCode": "ORDER1A2B3C4D5E",
                "productType": "ChatGPT Business Slot",
                "quantity": 1,
                "finalQuantity": 1,
                "slotMonths": 3,
                "customerEmail": "customer@example.com",
                "amountUsd": 6.0,
                "discountPercent": 0,
                "discountAmount": 0,
                "workspaceInviteStatus": "sent",
                "workspaceOwnerEmail": "owner@example.com",
                "deliveredAccounts": [
                    {
                        "productItemId": "item-1",
                        "user": "account@example.com",
                        "password": "secret-password",
                        "verifyEmail": "recovery@example.com",
                        "deliveredAt": "2026-03-24T10:15:00.000Z",
                    }
                ],
            },
        )

    client = canboso_client(handler)
    order = await client.create_order(
        "slot_chatgpt_business",
        quantity=1,
        purchase_options={
            "customer_email": "customer@example.com",
            "slot_months": 3,
        },
    )

    assert order.order_id == "ORDER1A2B3C4D5E"
    assert order.delivered is True
    assert order.amount == Decimal("6.00")
    assert "account@example.com" in order.delivery_payload
    assert "secret-password" in order.delivery_payload
    assert "customer@example.com" in order.delivery_payload
    assert "3 mes(es)" in order.delivery_payload

    await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "message", "error_type"),
    [
        (401, "Invalid API key", ProdSellerAuthenticationError),
        (400, "Wallet balance is not enough", ProdSellerInsufficientBalanceError),
        (409, "Inventory not enough", ProdSellerOutOfStockError),
    ],
)
async def test_canboso_maps_documented_errors(status: int, message: str, error_type: type) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"success": False, "message": message})

    client = canboso_client(handler)
    with pytest.raises(error_type):
        await client.create_order("product-1")
    await client.close()


@pytest.mark.asyncio
async def test_registry_builds_canboso_adapter(tmp_path) -> None:
    providers_file = tmp_path / "providers.json"
    providers_file.write_text(
        json.dumps(
            {
                "version": 1,
                "providers": [
                    {
                        "code": "canboso",
                        "name": "Canboso",
                        "adapter": "canboso_buyer_v1",
                        "enabled": True,
                        "base_url": "https://canboso.com",
                        "api_key": "tgb_test_key",
                        "markup_percent": "20",
                        "auto_sync_minutes": 0,
                        "order_poll_attempts": 1,
                        "order_poll_delay_seconds": 0,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    settings = Settings(
        BOT_TOKEN="1234567890:abcdefghijklmnopqrstuvwxyzABCDE",
        ADMIN_IDS="123456789",
        API_PROVIDERS_FILE=str(providers_file),
    )

    registry = build_provider_registry(settings)
    runtime = registry.get("canboso")
    assert runtime is not None
    assert isinstance(runtime.client, CanbosoBuyerClient)
    assert runtime.client.supports_order_status is False
    await registry.close()


@pytest.mark.asyncio
async def test_canboso_sync_preserves_manual_price_and_stores_requirements() -> None:
    remote = {"price": 2.0, "available": 5}

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/telegram-buyer/products"
        return httpx.Response(
            200,
            json={
                "success": True,
                "products": [
                    {
                        "_id": "slot_chatgpt_business",
                        "product_name": "Business Slot",
                        "usdPricing": remote["price"],
                        "requiresCustomerEmail": True,
                        "requiresSlotMonths": True,
                        "slotDurations": [1, 3, 6],
                        "slotPricingMode": "per_month",
                        "stats": {"available": remote["available"]},
                    }
                ],
            },
        )

    client = canboso_client(handler)
    engine, factory = create_engine_and_session_factory("sqlite+aiosqlite:///:memory:")
    await init_database(engine)

    async with factory() as session:
        result = await sync_provider_catalog(
            session,
            client,
            provider_code="canboso",
            markup_percent=Decimal("50"),
        )
        assert result.created == 1
        product = await session.scalar(select(Product))
        assert product is not None
        assert product.price == Decimal("3.00")
        assert product.provider_cost == Decimal("2.00")
        assert product.active is False
        options = product_provider_options(product)
        assert options.requires_customer_email is True
        assert options.requires_slot_months is True
        assert options.slot_durations == (1, 3, 6)
        product.price = Decimal("9.99")
        product.active = True
        await session.commit()

    remote["price"] = 5.0
    remote["available"] = 8
    async with factory() as session:
        await sync_provider_catalog(
            session,
            client,
            provider_code="canboso",
            markup_percent=Decimal("50"),
        )
        product = await session.scalar(select(Product))
        assert product is not None
        assert product.price == Decimal("9.99")
        assert product.active is True
        assert product.provider_cost == Decimal("5.00")
        assert product.provider_stock == 8

    await client.close()
    await engine.dispose()


@pytest.mark.asyncio
async def test_canboso_slot_purchase_keeps_history_delivery_and_exact_totals() -> None:
    requests_seen: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/products"):
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "products": [
                        {
                            "_id": "slot_chatgpt_business",
                            "product_name": "ChatGPT Business Slot",
                            "usdPricing": 2,
                            "requiresCustomerEmail": True,
                            "requiresSlotMonths": True,
                            "slotDurations": [1, 3, 6],
                            "quantityFixed": 1,
                            "slotPricingMode": "per_month",
                            "stats": {"available": 20},
                        }
                    ],
                },
            )
        if request.method == "POST" and request.url.path.endswith("/purchase"):
            body = json.loads(request.content)
            requests_seen.append(body)
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "orderCode": "CANBOSO-ORDER-1",
                    "productType": "ChatGPT Business Slot",
                    "quantity": 1,
                    "finalQuantity": 1,
                    "amountUsd": 6,
                    "discountPercent": 0,
                    "discountAmount": 0,
                    "customerEmail": body["customer_email"],
                    "slotMonths": body["slot_months"],
                    "workspaceInviteStatus": "sent",
                    "workspaceOwnerEmail": "owner@example.com",
                    "deliveredAccounts": [
                        {
                            "user": "delivered@example.com",
                            "password": "pass-123",
                            "verifyEmail": "verify@example.com",
                        }
                    ],
                },
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    client = canboso_client(handler)
    engine, factory = create_engine_and_session_factory("sqlite+aiosqlite:///:memory:")
    await init_database(engine)

    async with factory() as session:
        await sync_provider_catalog(
            session,
            client,
            provider_code="canboso",
            markup_percent=Decimal("50"),
            new_products_active=True,
        )
        product = await session.scalar(select(Product))
        assert product is not None
        session.add(User(telegram_id=900100, first_name="Buyer", balance=Decimal("20.00")))
        await session.commit()
        product_id = product.id

    result = await purchase_provider_product(
        factory,
        client,
        provider_code="canboso",
        telegram_id=900100,
        product_id=product_id,
        allow_below_cost=False,
        poll_attempts=1,
        poll_delay_seconds=0,
        purchase_options={
            "customer_email": "customer@example.com",
            "slot_months": 3,
        },
    )

    assert result.price == Decimal("9.00")
    assert result.new_balance == Decimal("11.00")
    assert "delivered@example.com" in result.stock_payload
    assert requests_seen == [
        {
            "key": "tgb_test_key",
            "product_id": "slot_chatgpt_business",
            "quantity": 1,
            "customer_email": "customer@example.com",
            "slot_months": 3,
        }
    ]

    async with factory() as session:
        user = await session.scalar(select(User).where(User.telegram_id == 900100))
        purchase = await session.scalar(select(ProviderPurchase))
        order = await session.scalar(select(Order))
        stock = await session.scalar(select(StockItem))
        assert user is not None and user.balance == Decimal("11.00")
        assert purchase is not None
        assert purchase.status == "delivered"
        assert purchase.expected_provider_cost == Decimal("6.00")
        assert purchase.actual_provider_amount == Decimal("6.00")
        assert purchase.request_payload is not None
        request_payload = json.loads(purchase.request_payload)
        assert request_payload == {
            "quantity": 1,
            "customer_email": "customer@example.com",
            "slot_months": 3,
        }
        assert order is not None and order.price == Decimal("9.00")
        assert order.provider_code == "canboso"
        assert order.provider_order_id == "CANBOSO-ORDER-1"
        assert stock is not None and "pass-123" in stock.payload

    await client.close()
    await engine.dispose()
