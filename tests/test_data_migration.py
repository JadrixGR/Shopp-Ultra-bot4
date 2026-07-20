from __future__ import annotations

import sqlite3
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.database import create_engine_and_session_factory, init_database
from app.models import (
    BalanceAdjustment,
    Broadcast,
    Deposit,
    Order,
    Product,
    ProviderPurchase,
    Refund,
    User,
)


@pytest.mark.asyncio
async def test_legacy_users_balances_history_and_deposits_survive_upgrade(tmp_path) -> None:
    db_path = tmp_path / "legacy_full.db"
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        PRAGMA foreign_keys=ON;
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
        CREATE TABLE stock_items (
            id INTEGER PRIMARY KEY,
            product_id INTEGER NOT NULL,
            payload TEXT NOT NULL,
            payload_hash VARCHAR(64) NOT NULL,
            status VARCHAR(16) NOT NULL DEFAULT 'available',
            sold_to_user_id INTEGER,
            created_at DATETIME NOT NULL,
            sold_at DATETIME,
            CONSTRAINT uq_stock_product_hash UNIQUE (product_id, payload_hash),
            FOREIGN KEY(product_id) REFERENCES products(id) ON DELETE CASCADE,
            FOREIGN KEY(sold_to_user_id) REFERENCES users(id) ON DELETE SET NULL
        );
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY,
            order_code VARCHAR(32) NOT NULL UNIQUE,
            user_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            stock_item_id INTEGER NOT NULL UNIQUE,
            product_name VARCHAR(180) NOT NULL,
            price NUMERIC(18, 2) NOT NULL,
            status VARCHAR(16) NOT NULL DEFAULT 'completed',
            created_at DATETIME NOT NULL,
            delivered_at DATETIME NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id),
            FOREIGN KEY(product_id) REFERENCES products(id),
            FOREIGN KEY(stock_item_id) REFERENCES stock_items(id)
        );
        CREATE TABLE deposits (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            requested_amount NUMERIC(18, 2) NOT NULL,
            credited_amount NUMERIC(18, 2) NOT NULL DEFAULT 0,
            bonus_amount NUMERIC(18, 2) NOT NULL DEFAULT 0,
            currency VARCHAR(12) NOT NULL DEFAULT 'USDT',
            status VARCHAR(16) NOT NULL DEFAULT 'pending',
            claimed_transaction_id VARCHAR(160),
            transaction_id VARCHAR(160) UNIQUE,
            verify_attempts INTEGER NOT NULL DEFAULT 0,
            last_verify_at DATETIME,
            failure_reason TEXT,
            raw_payload TEXT,
            created_at DATETIME NOT NULL,
            verified_at DATETIME,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        CREATE TABLE app_settings (
            key VARCHAR(64) PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at DATETIME NOT NULL
        );

        INSERT INTO users VALUES (
            1, 777001, 'cliente', 'Cliente', 'es', 84.25, 0,
            CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
        );
        INSERT INTO products VALUES (
            1, 'Producto anterior', 'Descripción guardada', 5.00, '📦',
            NULL, NULL, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
        );
        INSERT INTO stock_items VALUES (
            1, 1, 'correo@example.com:clave',
            'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
            'sold', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
        );
        INSERT INTO orders VALUES (
            1, 'ORD-ANTERIOR', 1, 1, 1, 'Producto anterior', 5.00,
            'completed', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
        );
        INSERT INTO deposits VALUES (
            1, 1, 50.00, 50.00, 1.00, 'USDT', 'credited',
            '123456789', 'M_P_123456789', 1, CURRENT_TIMESTAMP,
            NULL, '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
        );
        INSERT INTO app_settings VALUES ('store_name', 'Tienda anterior', CURRENT_TIMESTAMP);
        """
    )
    connection.commit()
    connection.close()

    engine, factory = create_engine_and_session_factory(f"sqlite+aiosqlite:///{db_path.as_posix()}")
    await init_database(engine)

    async with factory() as session:
        user = await session.scalar(select(User).where(User.telegram_id == 777001))
        product = await session.get(Product, 1)
        order = await session.scalar(select(Order).where(Order.order_code == "ORD-ANTERIOR"))
        deposit = await session.get(Deposit, 1)
        provider_table_count = int(
            await session.scalar(select(func.count(ProviderPurchase.id))) or 0
        )

        assert user is not None and user.balance == Decimal("84.25")
        assert product is not None and product.name == "Producto anterior"
        assert product.instructions == ""
        assert product.description_entities == "[]"
        assert product.instructions_entities == "[]"
        assert product.provider_code is None
        assert product.service_days is None
        assert order is not None and order.product_name == "Producto anterior"
        assert order.quantity == 1
        assert order.instructions_snapshot == ""
        assert order.instructions_entities_snapshot == "[]"
        assert order.provider_code is None
        assert order.refunded_amount == Decimal("0.00")
        assert order.refund_status == "none"
        assert deposit is not None and deposit.credited_amount == Decimal("50.00")
        assert provider_table_count == 0
        assert int(await session.scalar(select(func.count(Refund.id))) or 0) == 0
        assert int(await session.scalar(select(func.count(BalanceAdjustment.id))) or 0) == 0
        assert int(await session.scalar(select(func.count(Broadcast.id))) or 0) == 0

    raw = sqlite3.connect(db_path)
    product_columns = {row[1] for row in raw.execute("PRAGMA table_info(products)")}
    order_columns = {row[1] for row in raw.execute("PRAGMA table_info(orders)")}
    table_names = {
        row[0]
        for row in raw.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    raw.close()

    assert "provider_price_locked" in product_columns
    assert "service_days" in product_columns
    assert "instructions" in product_columns
    assert "description_entities" in product_columns
    assert "instructions_entities" in product_columns
    assert "provider_order_id" in order_columns
    assert "quantity" in order_columns
    assert "instructions_snapshot" in order_columns
    assert "instructions_entities_snapshot" in order_columns
    assert "refunded_amount" in order_columns
    assert "refund_status" in order_columns
    assert {
        "provider_purchases",
        "refunds",
        "balance_adjustments",
        "broadcasts",
    }.issubset(table_names)

    await engine.dispose()


@pytest.mark.asyncio
async def test_previous_multi_api_database_keeps_provider_sales_and_adds_canboso_columns(
    tmp_path,
) -> None:
    db_path = tmp_path / "previous_multi_api.db"
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        PRAGMA foreign_keys=ON;
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
            service_days INTEGER,
            provider_code VARCHAR(32),
            external_product_id VARCHAR(128),
            provider_cost NUMERIC(18, 2),
            provider_stock INTEGER,
            provider_in_stock BOOLEAN,
            provider_image_url TEXT,
            provider_price_locked BOOLEAN NOT NULL DEFAULT 1,
            provider_synced_at DATETIME,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            CONSTRAINT uq_product_provider_external_id UNIQUE(provider_code, external_product_id)
        );
        CREATE TABLE provider_purchases (
            id INTEGER PRIMARY KEY,
            purchase_code VARCHAR(40) NOT NULL UNIQUE,
            user_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            order_id INTEGER UNIQUE,
            provider_code VARCHAR(32) NOT NULL,
            provider_product_id VARCHAR(128) NOT NULL,
            provider_order_id VARCHAR(160),
            quantity INTEGER NOT NULL DEFAULT 1,
            local_price NUMERIC(18, 2) NOT NULL,
            expected_provider_cost NUMERIC(18, 2),
            actual_provider_amount NUMERIC(18, 2),
            status VARCHAR(24) NOT NULL DEFAULT 'processing',
            delivery_payload TEXT,
            error_message TEXT,
            raw_payload TEXT,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            completed_at DATETIME,
            FOREIGN KEY(user_id) REFERENCES users(id),
            FOREIGN KEY(product_id) REFERENCES products(id)
        );

        INSERT INTO users VALUES (
            1, 99887766, 'cliente_api', 'Cliente API', 'es', 33.50, 0,
            CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
        );
        INSERT INTO products VALUES (
            1, 'Producto API anterior', 'Entrega guardada', 9.99, '⚡',
            NULL, NULL, 1, NULL, 'proveedor_anterior', 'external-123',
            4.00, 8, 1, NULL, 1, CURRENT_TIMESTAMP,
            CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
        );
        INSERT INTO provider_purchases VALUES (
            1, 'API-ANTERIOR', 1, 1, NULL, 'proveedor_anterior', 'external-123',
            'REMOTE-ORDER-1', 1, 9.99, 4.00, 4.00, 'manual_review', NULL,
            'pendiente de revisión', '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, NULL
        );
        """
    )
    connection.commit()
    connection.close()

    engine, factory = create_engine_and_session_factory(f"sqlite+aiosqlite:///{db_path.as_posix()}")
    await init_database(engine)

    async with factory() as session:
        user = await session.scalar(select(User).where(User.telegram_id == 99887766))
        product = await session.get(Product, 1)
        purchase = await session.get(ProviderPurchase, 1)
        assert user is not None and user.balance == Decimal("33.50")
        assert product is not None and product.price == Decimal("9.99")
        assert product.provider_code == "proveedor_anterior"
        assert product.provider_metadata is None
        assert purchase is not None and purchase.purchase_code == "API-ANTERIOR"
        assert purchase.status == "manual_review"
        assert purchase.request_payload is None
        assert purchase.error_message == "pendiente de revisión"

    raw = sqlite3.connect(db_path)
    product_columns = {row[1] for row in raw.execute("PRAGMA table_info(products)")}
    purchase_columns = {row[1] for row in raw.execute("PRAGMA table_info(provider_purchases)")}
    raw.close()

    assert "provider_metadata" in product_columns
    assert "request_payload" in purchase_columns

    await engine.dispose()
