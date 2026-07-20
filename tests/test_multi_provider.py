from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select

from app.config import Settings
from app.database import create_engine_and_session_factory, init_database
from app.models import Product, User
from app.services.catalog import ProductWithStock
from app.services.external_purchases import purchase_provider_product
from app.services.prodseller import ProdSellerClient
from app.services.provider_catalog import sync_provider_catalog
from app.services.provider_registry import build_provider_registry, load_provider_configs


def client(handler, *, header: str = "X-API-Key", name: str = "Provider") -> ProdSellerClient:  # type: ignore[no-untyped-def]
    return ProdSellerClient(
        api_key="secret-key",
        base_url="https://provider.test/v1",
        allow_insecure_http=False,
        api_key_header=header,
        provider_name=name,
        cache_seconds=0,
        transport=httpx.MockTransport(handler),
    )


def test_customer_stock_text_never_discloses_api_source() -> None:
    product = Product(
        name="External",
        description="",
        price=Decimal("1.00"),
        provider_code="provider_one",
        external_product_id="p1",
        provider_in_stock=True,
    )
    item = ProductWithStock(product=product, stock=1, external_stock_known=False)
    assert item.stock_text("es") == "Disponible"
    assert item.stock_text("en") == "Available"
    assert "API" not in item.stock_text("es")


@pytest.mark.asyncio
async def test_two_providers_sync_into_one_products_table_and_preserve_selection_and_price() -> (
    None
):
    price_by_path = {
        "/v1/products": Decimal("2.00"),
    }

    async def handler_one(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-API-Key"] == "secret-key"
        return httpx.Response(
            200,
            json={
                "products": [
                    {
                        "id": "one-product",
                        "name": "Product one",
                        "price": float(price_by_path["/v1/products"]),
                        "inStock": True,
                    }
                ]
            },
        )

    async def handler_two(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "products": [
                    {
                        "id": "two-product",
                        "name": "Product two",
                        "price": 3,
                        "inStock": True,
                    }
                ]
            },
        )

    first = client(handler_one, name="First")
    second = client(handler_two, name="Second")
    engine, factory = create_engine_and_session_factory("sqlite+aiosqlite:///:memory:")
    await init_database(engine)

    async with factory() as session:
        await sync_provider_catalog(
            session,
            first,
            provider_code="first_api",
            markup_percent=Decimal("25"),
        )
        await sync_provider_catalog(
            session,
            second,
            provider_code="second_api",
            markup_percent=Decimal("10"),
        )
        products = (await session.scalars(select(Product).order_by(Product.provider_code))).all()
        assert len(products) == 2
        assert {product.provider_code for product in products} == {"first_api", "second_api"}
        assert all(product.active is False for product in products)

        first_product = next(
            product for product in products if product.provider_code == "first_api"
        )
        assert first_product.price == Decimal("2.50")
        first_product.price = Decimal("9.99")
        first_product.active = True
        await session.commit()

    price_by_path["/v1/products"] = Decimal("7.00")
    async with factory() as session:
        await sync_provider_catalog(
            session,
            first,
            provider_code="first_api",
            markup_percent=Decimal("25"),
        )
        first_product = await session.scalar(
            select(Product).where(Product.provider_code == "first_api")
        )
        assert first_product is not None
        assert first_product.provider_cost == Decimal("7.00")
        assert first_product.price == Decimal("9.99")
        assert first_product.active is True

    await first.close()
    await second.close()
    await engine.dispose()


@pytest.mark.asyncio
async def test_generic_provider_purchase_uses_product_provider_code() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/products/external-x"):
            return httpx.Response(
                200,
                json={
                    "id": "external-x",
                    "name": "External X",
                    "price": 2,
                    "stock": 2,
                    "delivery": {"type": "instant"},
                },
            )
        if request.method == "POST" and request.url.path.endswith("/orders"):
            return httpx.Response(
                200,
                json={
                    "orderId": "order-x",
                    "status": "delivered",
                    "product": {"id": "external-x", "name": "External X"},
                    "quantity": 1,
                    "amount": 2,
                    "deliveredKey": "user@example.com:password",
                },
            )
        raise AssertionError(request.url)

    provider = client(handler, name="Another shop")
    engine, factory = create_engine_and_session_factory("sqlite+aiosqlite:///:memory:")
    await init_database(engine)
    async with factory() as session:
        session.add_all(
            [
                User(
                    telegram_id=90001,
                    first_name="Buyer",
                    balance=Decimal("10.00"),
                ),
                Product(
                    name="External X",
                    description="",
                    price=Decimal("4.00"),
                    active=True,
                    provider_code="another_shop",
                    external_product_id="external-x",
                    provider_in_stock=True,
                ),
            ]
        )
        await session.commit()
        product = await session.scalar(select(Product))
        assert product is not None

    result = await purchase_provider_product(
        factory,
        provider,
        provider_code="another_shop",
        telegram_id=90001,
        product_id=product.id,
        allow_below_cost=False,
        poll_attempts=1,
        poll_delay_seconds=0,
    )
    assert result.stock_payload == "user@example.com:password"

    await provider.close()
    await engine.dispose()


@pytest.mark.asyncio
async def test_registry_loads_multiple_provider_connections_and_custom_headers(tmp_path) -> None:
    path = tmp_path / "providers.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "providers": [
                    {
                        "code": "shop_one",
                        "name": "Shop One",
                        "base_url": "https://one.example/v1",
                        "api_key": "key-one",
                        "api_key_header": "X-API-Key",
                    },
                    {
                        "code": "shop_two",
                        "name": "Shop Two",
                        "base_url": "https://two.example/v1",
                        "api_key": "key-two",
                        "api_key_header": "Authorization-Key",
                        "auto_sync_minutes": 0,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    settings = Settings(
        BOT_TOKEN="1234567890:abcdefghijklmnopqrstuvwxyzABCDE",
        ADMIN_IDS="123456789",
        API_PROVIDERS_FILE=str(path),
    )
    configs = load_provider_configs(settings)
    assert [config.code for config in configs] == ["shop_one", "shop_two"]
    assert configs[1].api_key_header == "Authorization-Key"

    registry = build_provider_registry(settings)
    assert len(registry) == 2
    assert registry.get("shop_one") is not None
    assert registry.get("shop_two") is not None
    assert dict(registry.items()).keys() == {"shop_one", "shop_two"}
    assert registry.keys() == ("shop_one", "shop_two")
    assert "shop_one" in registry
    assert "missing" not in registry
    await registry.close()


@pytest.mark.asyncio
async def test_provider_sync_reports_auto_published_and_restocked_products() -> None:
    remote_state = {"inStock": True, "price": 2.0}

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "products": [
                    {
                        "id": "notice-product",
                        "name": "Notice product",
                        "price": remote_state["price"],
                        "inStock": remote_state["inStock"],
                    }
                ]
            },
        )

    provider = client(handler, name="Notice provider")
    engine, factory = create_engine_and_session_factory("sqlite+aiosqlite:///:memory:")
    await init_database(engine)

    async with factory() as session:
        first = await sync_provider_catalog(
            session,
            provider,
            provider_code="notice_provider",
            markup_percent=Decimal("25"),
            new_products_active=True,
        )
        assert first.created == 1
        assert len(first.created_products) == 1
        assert len(first.published_products) == 1
        assert first.published_products[0].price == Decimal("2.50")

    remote_state["inStock"] = False
    async with factory() as session:
        second = await sync_provider_catalog(
            session,
            provider,
            provider_code="notice_provider",
            markup_percent=Decimal("25"),
        )
        assert second.restocked_products == ()

    remote_state["inStock"] = True
    remote_state["price"] = 5.0
    async with factory() as session:
        third = await sync_provider_catalog(
            session,
            provider,
            provider_code="notice_provider",
            markup_percent=Decimal("25"),
        )
        product = await session.scalar(
            select(Product).where(Product.provider_code == "notice_provider")
        )
        assert product is not None
        assert product.active is True
        assert product.provider_cost == Decimal("5.00")
        assert product.price == Decimal("2.50")
        assert len(third.restocked_products) == 1
        assert third.restocked_products[0].product_id == product.id

    await provider.close()
    await engine.dispose()
