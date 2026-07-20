from __future__ import annotations

import asyncio
import logging
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Product
from app.services.notifications import ProductNotice, broadcast_catalog_update
from app.services.prodseller import PROVIDER_CODE, ProdSellerClient, ProdSellerProduct
from app.services.provider_options import base_provider_cost, serialize_provider_options
from app.services.provider_registry import ProviderRuntime
from app.services.settings import get_provider_auto_publish
from app.utils import h, h_truncate, money

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ProviderProductChange:
    product_id: int
    name: str
    price: Decimal
    button_emoji: str = "🛍️"

    def notice(self) -> ProductNotice:
        return ProductNotice(
            product_id=self.product_id,
            name=self.name,
            price=self.price,
            button_emoji=self.button_emoji,
        )


@dataclass(frozen=True, slots=True)
class ProviderSyncResult:
    received: int
    created: int
    updated: int
    unavailable: int
    prices_updated: int = 0
    created_products: tuple[ProviderProductChange, ...] = ()
    published_products: tuple[ProviderProductChange, ...] = ()
    restocked_products: tuple[ProviderProductChange, ...] = ()


def retail_price(cost: Decimal, markup_percent: Decimal) -> Decimal:
    multiplier = Decimal("1") + (markup_percent / Decimal("100"))
    return (cost * multiplier).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _provider_stock(product: ProdSellerProduct) -> int | None:
    if product.stock is not None:
        return product.stock
    if not product.in_stock:
        return 0
    return None


def _available(*, in_stock: bool | None, stock: int | None) -> bool:
    if in_stock is False:
        return False
    return stock is None or stock > 0


async def sync_provider_catalog(
    session: AsyncSession,
    client: ProdSellerClient,
    *,
    provider_code: str,
    markup_percent: Decimal,
    force_refresh: bool = True,
    new_products_active: bool = False,
) -> ProviderSyncResult:
    """Synchronize provider metadata without overwriting store-owned fields.

    Public name, description, emoji, media, active selection and retail price are
    controlled by the administrator. Existing values remain untouched during
    every API refresh. New products can be imported inactive for manual selection
    or published automatically when the provider-specific option is enabled.
    """

    remote_products = await client.list_products(force_refresh=force_refresh)
    existing_rows = (
        await session.scalars(select(Product).where(Product.provider_code == provider_code))
    ).all()
    existing = {
        product.external_product_id: product
        for product in existing_rows
        if product.external_product_id
    }

    now = datetime.now(UTC)
    created = 0
    updated = 0
    seen: set[str] = set()
    created_rows: list[Product] = []
    published_rows: list[Product] = []
    restocked_rows: list[Product] = []

    for remote in remote_products:
        seen.add(remote.id)
        remote_stock = _provider_stock(remote)
        remote_cost = base_provider_cost(remote)
        remote_metadata = serialize_provider_options(remote)
        product = existing.get(remote.id)
        if product is None:
            publish_now = new_products_active and _available(
                in_stock=remote.in_stock,
                stock=remote_stock,
            )
            product = Product(
                name=remote.name[:180],
                description=(remote.description or "Producto con entrega automática.")[:3000],
                price=retail_price(remote_cost, markup_percent),
                button_emoji="⚡",
                media_type=None,
                media_file_id=None,
                active=publish_now,
                provider_code=provider_code,
                external_product_id=remote.id,
                provider_cost=remote_cost,
                provider_stock=remote_stock,
                provider_in_stock=remote.in_stock,
                provider_image_url=remote.image_url,
                provider_metadata=remote_metadata,
                provider_price_locked=True,
                provider_synced_at=now,
            )
            session.add(product)
            existing[remote.id] = product
            created_rows.append(product)
            if publish_now:
                published_rows.append(product)
            created += 1
            continue

        was_available = _available(
            in_stock=product.provider_in_stock,
            stock=product.provider_stock,
        )
        # Only provider-owned metadata is refreshed. Retail price and selection
        # intentionally stay exactly as the administrator left them.
        product.provider_cost = remote_cost
        product.provider_stock = remote_stock
        product.provider_in_stock = remote.in_stock
        product.provider_image_url = remote.image_url
        product.provider_metadata = remote_metadata
        product.provider_synced_at = now
        is_available = _available(in_stock=remote.in_stock, stock=remote_stock)
        if product.active and not was_available and is_available:
            restocked_rows.append(product)
        updated += 1

    unavailable = 0
    for external_id, product in existing.items():
        if external_id not in seen:
            product.provider_in_stock = False
            product.provider_stock = 0
            product.provider_synced_at = now
            unavailable += 1

    await session.flush()

    def changes(rows: list[Product]) -> tuple[ProviderProductChange, ...]:
        return tuple(
            ProviderProductChange(
                product_id=product.id,
                name=product.name,
                price=Decimal(product.price),
                button_emoji=product.button_emoji,
            )
            for product in rows
        )

    result = ProviderSyncResult(
        received=len(remote_products),
        created=created,
        updated=updated,
        unavailable=unavailable,
        prices_updated=0,
        created_products=changes(created_rows),
        published_products=changes(published_rows),
        restocked_products=changes(restocked_rows),
    )
    await session.commit()
    return result


async def refresh_provider_product(
    session: AsyncSession,
    client: ProdSellerClient,
    product: Product,
    *,
    provider_code: str,
    force_refresh: bool = False,
) -> ProdSellerProduct:
    if product.provider_code != provider_code or not product.external_product_id:
        raise ValueError("Product is not linked to the selected provider")
    remote = await client.get_product(product.external_product_id, force_refresh=force_refresh)
    product.provider_cost = base_provider_cost(remote)
    product.provider_stock = _provider_stock(remote)
    product.provider_in_stock = remote.in_stock
    product.provider_image_url = remote.image_url
    product.provider_metadata = serialize_provider_options(remote)
    product.provider_synced_at = datetime.now(UTC)
    await session.commit()
    return remote


async def notify_provider_sync_changes(
    bot: Bot,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    provider_name: str,
    result: ProviderSyncResult,
    admin_ids: Collection[int],
) -> None:
    if result.published_products:
        await broadcast_catalog_update(
            bot,
            session_factory,
            products=[item.notice() for item in result.published_products],
            restocked=False,
        )

    if result.restocked_products:
        await broadcast_catalog_update(
            bot,
            session_factory,
            products=[item.notice() for item in result.restocked_products],
            restocked=True,
        )

    published_ids = {item.product_id for item in result.published_products}
    inactive_new = [
        item for item in result.created_products if item.product_id not in published_ids
    ]
    if not inactive_new:
        return

    visible = inactive_new[:12]
    lines = "\n".join(
        f"• <b>{h_truncate(item.name, 260)}</b> — ${money(item.price)}" for item in visible
    )
    if len(inactive_new) > len(visible):
        lines += f"\n• y {len(inactive_new) - len(visible)} más"
    text = (
        f"🆕 <b>{h(provider_name)} agregó productos</b>\n\n"
        f"Se detectaron <b>{len(inactive_new)}</b> productos nuevos. Están desactivados para "
        "no publicarlos sin tu autorización.\n\n"
        f"{lines}\n\n"
        "Entra en /admin → Proveedores API → Seleccionar productos para publicarlos."
    )
    for admin_id in admin_ids:
        try:
            await bot.send_message(admin_id, text)
        except Exception:
            logger.exception("Could not notify admin %s about new provider products", admin_id)


async def provider_auto_sync_loop(
    session_factory: async_sessionmaker[AsyncSession],
    runtime: ProviderRuntime,
    *,
    bot: Bot | None = None,
    admin_ids: Collection[int] = (),
) -> None:
    interval_minutes = runtime.config.auto_sync_minutes
    if interval_minutes <= 0:
        return

    delay = max(60, interval_minutes * 60)
    while True:
        try:
            async with session_factory() as session:
                auto_publish = await get_provider_auto_publish(
                    session,
                    runtime.config.code,
                    default=False,
                )
                result = await sync_provider_catalog(
                    session,
                    runtime.client,
                    provider_code=runtime.config.code,
                    markup_percent=runtime.config.markup_percent,
                    force_refresh=True,
                    new_products_active=auto_publish,
                )
            logger.info(
                "%s catalog sync: received=%s created=%s updated=%s unavailable=%s",
                runtime.config.name,
                result.received,
                result.created,
                result.updated,
                result.unavailable,
            )
            if bot is not None and (result.created_products or result.restocked_products):
                await notify_provider_sync_changes(
                    bot,
                    session_factory,
                    provider_name=runtime.config.name,
                    result=result,
                    admin_ids=admin_ids,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Automatic catalog sync failed for %s", runtime.config.name)
        await asyncio.sleep(delay)


# Compatibility wrappers for installations and tests created before multi-API support.
async def sync_prodseller_catalog(
    session: AsyncSession,
    client: ProdSellerClient,
    *,
    markup_percent: Decimal,
    update_prices: bool,
    force_refresh: bool = True,
) -> ProviderSyncResult:
    del update_prices  # Existing prices are always preserved in this release.
    return await sync_provider_catalog(
        session,
        client,
        provider_code=PROVIDER_CODE,
        markup_percent=markup_percent,
        force_refresh=force_refresh,
        new_products_active=False,
    )


async def refresh_prodseller_product(
    session: AsyncSession,
    client: ProdSellerClient,
    product: Product,
    *,
    force_refresh: bool = False,
) -> ProdSellerProduct:
    return await refresh_provider_product(
        session,
        client,
        product,
        provider_code=PROVIDER_CODE,
        force_refresh=force_refresh,
    )


async def prodseller_auto_sync_loop(
    session_factory: async_sessionmaker[AsyncSession],
    client: ProdSellerClient,
    *,
    markup_percent: Decimal,
    update_prices: bool,
    interval_minutes: int,
) -> None:
    del update_prices
    from app.services.provider_registry import ProviderConfig

    runtime = ProviderRuntime(
        config=ProviderConfig.from_dict(
            {
                "code": PROVIDER_CODE,
                "name": "ProdSeller",
                "base_url": client.base_url,
                "api_key": "compatibility-runtime-key",
                "allow_insecure_http": client.base_url.lower().startswith("http://"),
                "markup_percent": str(markup_percent),
                "auto_sync_minutes": interval_minutes,
            }
        ),
        client=client,
    )
    await provider_auto_sync_loop(session_factory, runtime)
