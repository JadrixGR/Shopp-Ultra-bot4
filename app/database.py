from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


def create_engine_and_session_factory(
    database_url: str,
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(database_url, pool_pre_ping=True)

    if database_url.startswith("sqlite"):

        @event.listens_for(engine.sync_engine, "connect")
        def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:  # type: ignore[no-untyped-def]
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    return engine, factory


async def _sqlite_columns(connection, table_name: str) -> set[str]:  # type: ignore[no-untyped-def]
    rows = (await connection.execute(text(f'PRAGMA table_info("{table_name}")'))).mappings()
    return {str(row["name"]) for row in rows}


async def _migrate_sqlite(connection) -> None:  # type: ignore[no-untyped-def]
    """Apply additive migrations without touching existing customer data.

    The released bot uses SQLite and ``create_all`` does not add columns to an
    existing table. These ALTER statements are intentionally additive so an
    old ``data/shop.db`` can be copied into this release and upgraded on the
    first startup.
    """

    product_columns = await _sqlite_columns(connection, "products")
    product_additions = {
        "provider_code": "VARCHAR(32)",
        "external_product_id": "VARCHAR(128)",
        "provider_cost": "NUMERIC(18, 2)",
        "provider_stock": "INTEGER",
        "provider_in_stock": "BOOLEAN",
        "provider_image_url": "TEXT",
        "provider_metadata": "TEXT",
        "provider_price_locked": "BOOLEAN NOT NULL DEFAULT 1",
        "provider_synced_at": "DATETIME",
        "service_days": "INTEGER",
        "button_style": "VARCHAR(16) NOT NULL DEFAULT 'primary'",
        "instructions": "TEXT NOT NULL DEFAULT ''",
        "description_entities": "TEXT NOT NULL DEFAULT '[]'",
        "instructions_entities": "TEXT NOT NULL DEFAULT '[]'",
    }
    for column, definition in product_additions.items():
        if column not in product_columns:
            await connection.execute(
                text(f'ALTER TABLE "products" ADD COLUMN "{column}" {definition}')
            )

    order_columns = await _sqlite_columns(connection, "orders")
    order_additions = {
        "provider_code": "VARCHAR(32)",
        "provider_order_id": "VARCHAR(160)",
        "provider_amount": "NUMERIC(18, 2)",
        "provider_discount_percent": "NUMERIC(8, 2)",
        "provider_discount_amount": "NUMERIC(18, 2)",
        "refunded_amount": "NUMERIC(18, 2) NOT NULL DEFAULT 0",
        "refund_status": "VARCHAR(16) NOT NULL DEFAULT 'none'",
        "quantity": "INTEGER NOT NULL DEFAULT 1",
        "instructions_snapshot": "TEXT NOT NULL DEFAULT ''",
        "instructions_entities_snapshot": "TEXT NOT NULL DEFAULT '[]'",
    }
    for column, definition in order_additions.items():
        if column not in order_columns:
            await connection.execute(
                text(f'ALTER TABLE "orders" ADD COLUMN "{column}" {definition}')
            )

    provider_purchase_columns = await _sqlite_columns(connection, "provider_purchases")
    provider_purchase_additions = {
        "request_payload": "TEXT",
    }
    for column, definition in provider_purchase_additions.items():
        if column not in provider_purchase_columns:
            await connection.execute(
                text(f'ALTER TABLE "provider_purchases" ADD COLUMN "{column}" {definition}')
            )

    await connection.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "uq_products_provider_external_id "
            "ON products(provider_code, external_product_id) "
            "WHERE provider_code IS NOT NULL AND external_product_id IS NOT NULL"
        )
    )
    await connection.execute(
        text("CREATE INDEX IF NOT EXISTS ix_orders_provider_order_id ON orders(provider_order_id)")
    )
    await connection.execute(
        text("CREATE INDEX IF NOT EXISTS ix_refunds_order_id ON refunds(order_id)")
    )
    await connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_balance_adjustments_user_id ON balance_adjustments(user_id)"
        )
    )
    await connection.execute(
        text("CREATE INDEX IF NOT EXISTS ix_broadcasts_status ON broadcasts(status)")
    )


async def init_database(engine: AsyncEngine) -> None:
    # Importing models registers all tables on Base.metadata.
    from app import models  # noqa: F401

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        if engine.url.get_backend_name() == "sqlite":
            await _migrate_sqlite(connection)


async def session_scope(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with factory() as session:
        yield session
