from __future__ import annotations

import secrets
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Order, Product, StockItem, User, utcnow

MAX_PURCHASE_QUANTITY = 100


class PurchaseError(Exception):
    pass


class ProductUnavailable(PurchaseError):
    pass


class OutOfStock(PurchaseError):
    pass


class InsufficientBalance(PurchaseError):
    pass


class InvalidQuantity(PurchaseError):
    pass


@dataclass(frozen=True, slots=True)
class PurchaseResult:
    order_code: str
    product_name: str
    price: Decimal
    stock_payload: str
    new_balance: Decimal
    quantity: int = 1
    order_codes: tuple[str, ...] = ()
    instructions: str = ""
    instructions_entities: str = "[]"


def _order_code() -> str:
    return f"ORD-{secrets.token_hex(4).upper()}"


def _normalize_quantity(quantity: int) -> int:
    try:
        normalized = int(quantity)
    except (TypeError, ValueError) as exc:
        raise InvalidQuantity from exc
    if normalized < 1 or normalized > MAX_PURCHASE_QUANTITY:
        raise InvalidQuantity
    return normalized


def _combine_payloads(payloads: list[str]) -> str:
    if len(payloads) == 1:
        return payloads[0]
    return "\n\n".join(
        f"===== PRODUCTO {index}/{len(payloads)} =====\n{payload}"
        for index, payload in enumerate(payloads, start=1)
    )


async def purchase_product(
    session: AsyncSession,
    *,
    telegram_id: int,
    product_id: int,
    quantity: int = 1,
) -> PurchaseResult:
    """Atomically deduct the total and claim the requested local stock units."""

    requested = _normalize_quantity(quantity)
    async with session.begin():
        user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
        product = await session.get(Product, product_id)
        if user is None or product is None or not product.active or product.is_external:
            raise ProductUnavailable

        unit_price = Decimal(product.price)
        total_price = unit_price * requested
        balance_result = await session.execute(
            update(User)
            .where(User.id == user.id, User.balance >= total_price)
            .values(balance=User.balance - total_price)
            .returning(User.balance)
        )
        new_balance = balance_result.scalar_one_or_none()
        if new_balance is None:
            raise InsufficientBalance

        candidate_ids = list(
            (
                await session.scalars(
                    select(StockItem.id)
                    .where(
                        StockItem.product_id == product.id,
                        StockItem.status == "available",
                    )
                    .order_by(StockItem.id)
                    .limit(requested)
                )
            ).all()
        )
        if len(candidate_ids) != requested:
            raise OutOfStock

        stock_result = await session.execute(
            update(StockItem)
            .where(
                StockItem.id.in_(candidate_ids),
                StockItem.status == "available",
            )
            .values(
                status="sold",
                sold_to_user_id=user.id,
                sold_at=utcnow(),
            )
            .returning(StockItem.id, StockItem.payload)
        )
        stock_rows = sorted(stock_result.all(), key=lambda row: int(row.id))
        if len(stock_rows) != requested:
            raise OutOfStock

        order_codes: list[str] = []
        payloads: list[str] = []
        for stock_row in stock_rows:
            code = _order_code()
            order_codes.append(code)
            payloads.append(str(stock_row.payload))
            session.add(
                Order(
                    order_code=code,
                    user_id=user.id,
                    product_id=product.id,
                    stock_item_id=stock_row.id,
                    product_name=product.name,
                    price=unit_price,
                    quantity=1,
                    instructions_snapshot=product.instructions,
                    instructions_entities_snapshot=product.instructions_entities,
                    status="completed",
                )
            )
        await session.flush()

        return PurchaseResult(
            order_code=order_codes[0],
            product_name=product.name,
            price=total_price,
            stock_payload=_combine_payloads(payloads),
            new_balance=Decimal(new_balance),
            quantity=requested,
            order_codes=tuple(order_codes),
            instructions=product.instructions,
            instructions_entities=product.instructions_entities,
        )
