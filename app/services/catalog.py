from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Product, StockItem


@dataclass(frozen=True, slots=True)
class ProductWithStock:
    product: Product
    stock: int
    external_stock_known: bool = False

    @property
    def is_external(self) -> bool:
        return self.product.is_external

    @property
    def available(self) -> bool:
        return self.stock > 0

    def stock_text(self, language: str = "es") -> str:
        if not self.is_external:
            return str(self.stock)
        if not self.available:
            return "Agotado" if language == "es" else "Sold out"
        if self.external_stock_known:
            return str(self.stock)
        return "Disponible" if language == "es" else "Available"


def _effective_stock(product: Product, local_stock: int) -> tuple[int, bool]:
    if not product.is_external:
        return local_stock, True
    if product.provider_in_stock is False:
        return 0, product.provider_stock is not None
    if product.provider_stock is not None:
        return max(0, int(product.provider_stock)), True
    return (1 if product.provider_in_stock is not False else 0), False


async def list_active_products(
    session: AsyncSession,
    *,
    page: int = 0,
    page_size: int | None = 8,
) -> tuple[list[ProductWithStock], int]:
    """Return active products together with the total active-product count.

    ``page_size=None`` disables pagination and returns the complete active
    catalog in a single query. The optional paginated behavior is retained for
    backwards compatibility with internal callers and older tests.
    """

    page = max(page, 0)
    stock_subquery = (
        select(
            StockItem.product_id.label("product_id"),
            func.count(StockItem.id).label("stock"),
        )
        .where(StockItem.status == "available")
        .group_by(StockItem.product_id)
        .subquery()
    )
    total = await session.scalar(select(func.count(Product.id)).where(Product.active.is_(True)))
    query = (
        select(Product, func.coalesce(stock_subquery.c.stock, 0))
        .outerjoin(stock_subquery, Product.id == stock_subquery.c.product_id)
        .where(Product.active.is_(True))
        .order_by(Product.id.desc())
    )
    if page_size is not None:
        normalized_page_size = max(1, page_size)
        query = query.offset(page * normalized_page_size).limit(normalized_page_size)

    rows = (await session.execute(query)).all()
    result: list[ProductWithStock] = []
    for product, local_stock_raw in rows:
        stock, known = _effective_stock(product, int(local_stock_raw))
        result.append(ProductWithStock(product=product, stock=stock, external_stock_known=known))
    return result, int(total or 0)


async def list_all_products(session: AsyncSession) -> list[ProductWithStock]:
    stock_subquery = (
        select(
            StockItem.product_id.label("product_id"),
            func.count(StockItem.id).label("stock"),
        )
        .where(StockItem.status == "available")
        .group_by(StockItem.product_id)
        .subquery()
    )
    rows = (
        await session.execute(
            select(Product, func.coalesce(stock_subquery.c.stock, 0))
            .outerjoin(stock_subquery, Product.id == stock_subquery.c.product_id)
            .order_by(Product.id.desc())
        )
    ).all()
    result: list[ProductWithStock] = []
    for product, local_stock_raw in rows:
        stock, known = _effective_stock(product, int(local_stock_raw))
        result.append(ProductWithStock(product=product, stock=stock, external_stock_known=known))
    return result


async def get_product_with_stock(session: AsyncSession, product_id: int) -> ProductWithStock | None:
    product = await session.get(Product, product_id)
    if product is None:
        return None
    local_stock = await session.scalar(
        select(func.count(StockItem.id)).where(
            StockItem.product_id == product_id,
            StockItem.status == "available",
        )
    )
    stock, known = _effective_stock(product, int(local_stock or 0))
    return ProductWithStock(product=product, stock=stock, external_stock_known=known)


async def create_product(
    session: AsyncSession,
    *,
    name: str,
    description: str,
    description_entities: str = "[]",
    instructions: str = "",
    instructions_entities: str = "[]",
    price: Decimal,
    button_emoji: str,
    media_type: str | None,
    media_file_id: str | None,
    button_style: str = "primary",
) -> Product:
    product = Product(
        name=name.strip(),
        description=description.strip(),
        description_entities=description_entities or "[]",
        instructions=instructions.strip(),
        instructions_entities=instructions_entities or "[]",
        price=price.quantize(Decimal("0.01")),
        button_emoji=(button_emoji.strip() or "🛍️")[:32],
        button_style=button_style
        if button_style in {"primary", "success", "danger", "default"}
        else "primary",
        media_type=media_type,
        media_file_id=media_file_id,
        active=True,
    )
    session.add(product)
    await session.flush()
    await session.commit()
    return product


STOCK_ITEM_DELIMITER = "--"


def split_stock_payloads(text: str) -> list[str]:
    """Split imported stock while supporting multiline credentials.

    Backwards-compatible mode: without a delimiter, every non-empty line is one
    stock unit. Multiline mode is enabled when a line contains exactly ``--``;
    each block between delimiter lines becomes one stock unit and internal line
    breaks are preserved.
    """

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    delimiter_present = any(line.strip() == STOCK_ITEM_DELIMITER for line in lines)
    if not delimiter_present:
        return [line.strip() for line in lines if line.strip()]

    items: list[str] = []
    block: list[str] = []

    def flush() -> None:
        while block and not block[0].strip():
            block.pop(0)
        while block and not block[-1].strip():
            block.pop()
        payload = "\n".join(line.rstrip() for line in block).strip()
        if payload:
            items.append(payload)
        block.clear()

    for line in lines:
        if line.strip() == STOCK_ITEM_DELIMITER:
            flush()
        else:
            block.append(line)
    flush()
    return items


async def add_stock_items(
    session: AsyncSession, product_id: int, payloads: list[str]
) -> tuple[int, int]:
    """Insert many local stock units efficiently and report omitted duplicates."""

    product = await session.get(Product, product_id)
    if product is None:
        return 0, 0
    if product.is_external:
        raise ValueError("External API products do not accept local stock")

    cleaned: dict[str, str] = {}
    received_count = 0
    for payload in payloads:
        normalized = payload.strip()
        if not normalized:
            continue
        received_count += 1
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        cleaned.setdefault(digest, normalized)
    if not cleaned:
        return 0, 0

    hashes = list(cleaned)
    existing: set[str] = set()
    chunk_size = 500
    for offset in range(0, len(hashes), chunk_size):
        chunk = hashes[offset : offset + chunk_size]
        existing.update(
            (
                await session.scalars(
                    select(StockItem.payload_hash).where(
                        StockItem.product_id == product_id,
                        StockItem.payload_hash.in_(chunk),
                    )
                )
            ).all()
        )

    new_items = [
        StockItem(
            product_id=product_id,
            payload=payload,
            payload_hash=digest,
            status="available",
        )
        for digest, payload in cleaned.items()
        if digest not in existing
    ]
    session.add_all(new_items)
    await session.commit()

    duplicate_count = received_count - len(new_items)
    return len(new_items), duplicate_count
