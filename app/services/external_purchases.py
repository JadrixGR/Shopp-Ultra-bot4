from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Order, Product, ProviderPurchase, StockItem, User, utcnow
from app.services.prodseller import (
    PROVIDER_CODE,
    ProdSellerAmbiguousOrderError,
    ProdSellerAPIError,
    ProdSellerAuthenticationError,
    ProdSellerBadRequestError,
    ProdSellerClient,
    ProdSellerInsufficientBalanceError,
    ProdSellerNotFoundError,
    ProdSellerOrder,
    ProdSellerOutOfStockError,
    ProdSellerProduct,
    ProdSellerRateLimitError,
    ProdSellerServerError,
    ProdSellerTransportError,
)
from app.services.provider_options import (
    ProviderProductOptions,
    base_provider_cost,
    effective_provider_cost,
    effective_retail_price,
)
from app.services.purchases import (
    InsufficientBalance,
    ProductUnavailable,
    PurchaseResult,
)


class ExternalPurchaseError(Exception):
    pass


class ExternalProviderNotConfigured(ExternalPurchaseError):
    pass


class ExternalOutOfStock(ExternalPurchaseError):
    pass


class ExternalProviderBalanceLow(ExternalPurchaseError):
    pass


class ExternalProviderAuthenticationFailed(ExternalPurchaseError):
    pass


class ExternalProviderRateLimited(ExternalPurchaseError):
    pass


class ExternalProviderUnavailable(ExternalPurchaseError):
    pass


class ExternalRetailPriceBelowCost(ExternalPurchaseError):
    def __init__(self, retail_price: Decimal, provider_cost: Decimal) -> None:
        super().__init__("Retail price is below provider cost")
        self.retail_price = retail_price
        self.provider_cost = provider_cost


class ExternalOrderRejected(ExternalPurchaseError):
    pass


class ExternalPurchaseOptionsInvalid(ExternalPurchaseError):
    pass


@dataclass(frozen=True, slots=True)
class ExternalPurchaseQuote:
    requested_quantity: int
    quantity: int
    local_price: Decimal
    provider_cost: Decimal
    purchase_options: dict[str, object]


@dataclass(frozen=True, slots=True)
class ExternalOrderPending(ExternalPurchaseError):
    purchase_id: int
    purchase_code: str
    provider_order_id: str
    status: str


@dataclass(frozen=True, slots=True)
class ExternalOrderManualReview(ExternalPurchaseError):
    purchase_id: int
    purchase_code: str
    reason: str


@dataclass(frozen=True, slots=True)
class ExternalReservation:
    purchase_id: int
    purchase_code: str
    user_id: int
    product_id: int
    product_name: str
    provider_product_id: str
    quantity: int
    local_price: Decimal
    provider_cost: Decimal
    new_balance: Decimal


def _purchase_code() -> str:
    return f"API-{secrets.token_hex(5).upper()}"


def _order_code() -> str:
    return f"ORD-{secrets.token_hex(4).upper()}"


def quote_external_purchase(
    *,
    retail_unit_price: Decimal,
    provider_product: ProdSellerProduct,
    purchase_options: dict[str, object] | None = None,
    requested_quantity: int = 1,
) -> ExternalPurchaseQuote:
    """Validate provider-specific options and calculate the exact local/provider totals."""

    provider_options = ProviderProductOptions.from_remote(provider_product)
    try:
        normalized = provider_options.normalize_purchase_options(purchase_options)
        selected_quantity = provider_options.normalize_requested_quantity(requested_quantity)
    except ProdSellerBadRequestError as exc:
        raise ExternalPurchaseOptionsInvalid(str(exc)) from exc

    quantity = provider_options.provider_quantity(selected_quantity)
    local_price = effective_retail_price(
        Decimal(retail_unit_price),
        provider_options,
        normalized,
        requested_quantity=selected_quantity,
    )
    provider_cost = effective_provider_cost(
        provider_product,
        normalized,
        requested_quantity=selected_quantity,
    )
    return ExternalPurchaseQuote(
        requested_quantity=selected_quantity,
        quantity=quantity,
        local_price=local_price,
        provider_cost=provider_cost,
        purchase_options=normalized,
    )


async def _reserve_purchase(
    session: AsyncSession,
    *,
    telegram_id: int,
    product_id: int,
    provider_code: str,
    provider_cost: Decimal,
    provider_catalog_cost: Decimal,
    local_price: Decimal,
    quantity: int,
    request_payload: str | None,
    provider_in_stock: bool,
    provider_stock: int | None,
    provider_image_url: str | None,
    allow_below_cost: bool,
) -> ExternalReservation:
    async with session.begin():
        user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
        product = await session.get(Product, product_id)
        if (
            user is None
            or product is None
            or not product.active
            or product.provider_code != provider_code
            or not product.external_product_id
        ):
            raise ProductUnavailable

        product.provider_cost = provider_catalog_cost
        product.provider_in_stock = provider_in_stock
        product.provider_stock = provider_stock
        product.provider_image_url = provider_image_url
        product.provider_synced_at = utcnow()

        if not provider_in_stock or (provider_stock is not None and provider_stock < quantity):
            raise ExternalOutOfStock
        local_price = Decimal(local_price)
        if not allow_below_cost and local_price < provider_cost:
            raise ExternalRetailPriceBelowCost(local_price, provider_cost)

        balance_result = await session.execute(
            update(User)
            .where(User.id == user.id, User.balance >= local_price)
            .values(balance=User.balance - local_price)
            .returning(User.balance)
        )
        new_balance = balance_result.scalar_one_or_none()
        if new_balance is None:
            raise InsufficientBalance

        purchase = ProviderPurchase(
            purchase_code=_purchase_code(),
            user_id=user.id,
            product_id=product.id,
            provider_code=provider_code,
            provider_product_id=product.external_product_id,
            quantity=quantity,
            local_price=local_price,
            expected_provider_cost=provider_cost,
            status="processing",
            request_payload=request_payload,
        )
        session.add(purchase)
        await session.flush()
        return ExternalReservation(
            purchase_id=purchase.id,
            purchase_code=purchase.purchase_code,
            user_id=user.id,
            product_id=product.id,
            product_name=product.name,
            provider_product_id=product.external_product_id,
            quantity=quantity,
            local_price=local_price,
            provider_cost=provider_cost,
            new_balance=Decimal(new_balance),
        )


async def _record_provider_order(
    session: AsyncSession,
    *,
    purchase_id: int,
    order: ProdSellerOrder,
    status: str | None = None,
) -> None:
    async with session.begin():
        purchase = await session.get(ProviderPurchase, purchase_id)
        if purchase is None:
            return
        purchase.provider_order_id = order.order_id
        purchase.actual_provider_amount = order.amount
        purchase.status = status or order.status or "processing"
        purchase.raw_payload = ProdSellerClient.serialize_raw(order.raw)


async def refund_provider_purchase(
    session: AsyncSession,
    *,
    purchase_id: int,
    reason: str,
) -> bool:
    async with session.begin():
        purchase = await session.get(ProviderPurchase, purchase_id)
        if purchase is None or purchase.status in {"delivered", "refunded"}:
            return False
        user = await session.get(User, purchase.user_id)
        if user is None:
            return False
        user.balance = Decimal(user.balance) + Decimal(purchase.local_price)
        purchase.status = "refunded"
        purchase.error_message = reason[:4000]
        purchase.completed_at = utcnow()
        return True


async def mark_provider_manual_review(
    session: AsyncSession,
    *,
    purchase_id: int,
    reason: str,
    provider_order_id: str | None = None,
    raw_payload: str | None = None,
) -> None:
    async with session.begin():
        purchase = await session.get(ProviderPurchase, purchase_id)
        if purchase is None or purchase.status in {"delivered", "refunded"}:
            return
        purchase.status = "manual_review"
        purchase.error_message = reason[:4000]
        if provider_order_id:
            purchase.provider_order_id = provider_order_id
        if raw_payload:
            purchase.raw_payload = raw_payload


async def _finalize_delivery(
    session: AsyncSession,
    *,
    purchase_id: int,
    order: ProdSellerOrder,
) -> PurchaseResult:
    if not order.delivered:
        raise ExternalOrderRejected("Provider order does not contain a delivered key")

    async with session.begin():
        purchase = await session.get(ProviderPurchase, purchase_id)
        if purchase is None:
            raise ExternalOrderRejected("Local provider purchase does not exist")

        if purchase.order_id is not None:
            existing_order = await session.get(Order, purchase.order_id)
            if existing_order is None:
                raise ExternalOrderRejected("Linked local order is missing")
            stock = await session.get(StockItem, existing_order.stock_item_id)
            user = await session.get(User, purchase.user_id)
            if stock is None or user is None:
                raise ExternalOrderRejected("Existing delivery record is incomplete")
            return PurchaseResult(
                order_code=existing_order.order_code,
                product_name=existing_order.product_name,
                price=Decimal(existing_order.price),
                stock_payload=stock.payload,
                new_balance=Decimal(user.balance),
                quantity=max(1, int(existing_order.quantity or purchase.quantity or 1)),
                order_codes=(existing_order.order_code,),
                instructions=existing_order.instructions_snapshot,
                instructions_entities=existing_order.instructions_entities_snapshot,
            )

        user = await session.get(User, purchase.user_id)
        product = await session.get(Product, purchase.product_id)
        if user is None or product is None:
            raise ExternalOrderRejected("User or product is missing")

        payload = order.delivery_payload.strip()
        digest = hashlib.sha256(
            f"{purchase.provider_code}|{order.order_id}|{payload}".encode()
        ).hexdigest()
        stock_item = StockItem(
            product_id=product.id,
            payload=payload,
            payload_hash=digest,
            status="sold",
            sold_to_user_id=user.id,
            sold_at=utcnow(),
        )
        session.add(stock_item)
        await session.flush()

        local_order = Order(
            order_code=_order_code(),
            user_id=user.id,
            product_id=product.id,
            stock_item_id=stock_item.id,
            product_name=product.name,
            price=Decimal(purchase.local_price),
            quantity=max(1, int(purchase.quantity or order.quantity or 1)),
            instructions_snapshot=product.instructions,
            instructions_entities_snapshot=product.instructions_entities,
            status="completed",
            provider_code=purchase.provider_code,
            provider_order_id=order.order_id,
            provider_amount=order.amount,
            provider_discount_percent=order.discount_percent,
            provider_discount_amount=order.discount_amount,
        )
        session.add(local_order)
        await session.flush()

        purchase.order_id = local_order.id
        purchase.provider_order_id = order.order_id
        purchase.actual_provider_amount = order.amount
        purchase.status = "delivered"
        purchase.delivery_payload = payload
        purchase.raw_payload = ProdSellerClient.serialize_raw(order.raw)
        purchase.error_message = None
        purchase.completed_at = utcnow()

        if product.provider_stock is not None:
            product.provider_stock = max(0, int(product.provider_stock) - order.quantity)
            product.provider_in_stock = product.provider_stock > 0

        return PurchaseResult(
            order_code=local_order.order_code,
            product_name=product.name,
            price=Decimal(purchase.local_price),
            stock_payload=payload,
            new_balance=Decimal(user.balance),
            quantity=max(1, int(purchase.quantity or order.quantity or 1)),
            order_codes=(local_order.order_code,),
            instructions=product.instructions,
            instructions_entities=product.instructions_entities,
        )


async def purchase_provider_product(
    session_factory: async_sessionmaker[AsyncSession],
    client: ProdSellerClient,
    *,
    provider_code: str,
    telegram_id: int,
    product_id: int,
    allow_below_cost: bool,
    poll_attempts: int,
    poll_delay_seconds: float,
    purchase_options: dict[str, object] | None = None,
    requested_quantity: int = 1,
) -> PurchaseResult:
    # Verify current provider stock and cost before touching the customer balance.
    async with session_factory() as session:
        product = await session.get(Product, product_id)
        if (
            product is None
            or not product.active
            or product.provider_code != provider_code
            or not product.external_product_id
        ):
            raise ProductUnavailable
        external_product_id = product.external_product_id

    try:
        remote = await client.get_product(external_product_id, force_refresh=True)
    except ProdSellerOutOfStockError as exc:
        raise ExternalOutOfStock from exc
    except (ProdSellerNotFoundError, ProdSellerBadRequestError) as exc:
        raise ExternalOutOfStock from exc
    except ProdSellerAuthenticationError as exc:
        raise ExternalProviderAuthenticationFailed from exc
    except ProdSellerRateLimitError as exc:
        raise ExternalProviderRateLimited from exc
    except (ProdSellerTransportError, ProdSellerServerError, ProdSellerAPIError) as exc:
        raise ExternalProviderUnavailable from exc

    quote = quote_external_purchase(
        retail_unit_price=Decimal(product.price),
        provider_product=remote,
        purchase_options=purchase_options,
        requested_quantity=requested_quantity,
    )
    request_payload = ProdSellerClient.serialize_raw(
        {
            "quantity": quote.quantity,
            **quote.purchase_options,
        }
    )

    async with session_factory() as session:
        reservation = await _reserve_purchase(
            session,
            telegram_id=telegram_id,
            product_id=product_id,
            provider_code=provider_code,
            provider_cost=quote.provider_cost,
            provider_catalog_cost=base_provider_cost(remote),
            local_price=quote.local_price,
            quantity=quote.quantity,
            request_payload=request_payload,
            provider_in_stock=remote.in_stock,
            provider_stock=remote.stock,
            provider_image_url=remote.image_url,
            allow_below_cost=allow_below_cost,
        )

    try:
        order = await client.create_order(
            reservation.provider_product_id,
            quantity=reservation.quantity,
            purchase_options=quote.purchase_options,
        )
    except ProdSellerAmbiguousOrderError as exc:
        async with session_factory() as session:
            await mark_provider_manual_review(
                session,
                purchase_id=reservation.purchase_id,
                reason=str(exc),
            )
        raise ExternalOrderManualReview(
            purchase_id=reservation.purchase_id,
            purchase_code=reservation.purchase_code,
            reason=str(exc),
        ) from exc
    except ProdSellerOutOfStockError as exc:
        async with session_factory() as session:
            product = await session.get(Product, reservation.product_id)
            if product is not None:
                product.provider_in_stock = False
                product.provider_stock = 0
                await session.commit()
            await refund_provider_purchase(
                session,
                purchase_id=reservation.purchase_id,
                reason=f"provider_out_of_stock:{exc}",
            )
        raise ExternalOutOfStock from exc
    except ProdSellerInsufficientBalanceError as exc:
        async with session_factory() as session:
            await refund_provider_purchase(
                session,
                purchase_id=reservation.purchase_id,
                reason=f"provider_balance:{exc}",
            )
        raise ExternalProviderBalanceLow from exc
    except ProdSellerAuthenticationError as exc:
        async with session_factory() as session:
            await refund_provider_purchase(
                session,
                purchase_id=reservation.purchase_id,
                reason=f"provider_auth:{exc}",
            )
        raise ExternalProviderAuthenticationFailed from exc
    except ProdSellerRateLimitError as exc:
        async with session_factory() as session:
            await refund_provider_purchase(
                session,
                purchase_id=reservation.purchase_id,
                reason=f"provider_rate_limit:{exc}",
            )
        raise ExternalProviderRateLimited from exc
    except (ProdSellerBadRequestError, ProdSellerNotFoundError) as exc:
        async with session_factory() as session:
            await refund_provider_purchase(
                session,
                purchase_id=reservation.purchase_id,
                reason=f"provider_rejected:{exc}",
            )
        raise ExternalOrderRejected(str(exc)) from exc
    except (ProdSellerTransportError, ProdSellerServerError, ProdSellerAPIError) as exc:
        # A non-transport explicit API error is normally safe to refund. Server
        # failures are conservatively left for review because the POST may have
        # been processed before the provider returned an error.
        if isinstance(exc, ProdSellerServerError):
            async with session_factory() as session:
                await mark_provider_manual_review(
                    session,
                    purchase_id=reservation.purchase_id,
                    reason=f"provider_server_error:{exc}",
                )
            raise ExternalOrderManualReview(
                purchase_id=reservation.purchase_id,
                purchase_code=reservation.purchase_code,
                reason=str(exc),
            ) from exc
        async with session_factory() as session:
            await refund_provider_purchase(
                session,
                purchase_id=reservation.purchase_id,
                reason=f"provider_error:{exc}",
            )
        raise ExternalProviderUnavailable from exc

    async with session_factory() as session:
        await _record_provider_order(
            session,
            purchase_id=reservation.purchase_id,
            order=order,
        )

    try:
        final_order = await client.wait_for_delivery(
            order,
            attempts=poll_attempts,
            delay_seconds=poll_delay_seconds,
        )
    except (ProdSellerTransportError, ProdSellerServerError, ProdSellerAPIError) as exc:
        async with session_factory() as session:
            await mark_provider_manual_review(
                session,
                purchase_id=reservation.purchase_id,
                reason=f"status_check_error:{exc}",
                provider_order_id=order.order_id,
                raw_payload=ProdSellerClient.serialize_raw(order.raw),
            )
        raise ExternalOrderManualReview(
            purchase_id=reservation.purchase_id,
            purchase_code=reservation.purchase_code,
            reason=str(exc),
        ) from exc

    if final_order.delivered:
        try:
            async with session_factory() as session:
                return await _finalize_delivery(
                    session,
                    purchase_id=reservation.purchase_id,
                    order=final_order,
                )
        except ExternalOrderRejected as exc:
            async with session_factory() as session:
                await mark_provider_manual_review(
                    session,
                    purchase_id=reservation.purchase_id,
                    reason=f"delivery_finalize_error:{exc}",
                    provider_order_id=final_order.order_id,
                    raw_payload=ProdSellerClient.serialize_raw(final_order.raw),
                )
            raise ExternalOrderManualReview(
                purchase_id=reservation.purchase_id,
                purchase_code=reservation.purchase_code,
                reason=str(exc),
            ) from exc

    if final_order.status in {"failed", "cancelled", "refunded"}:
        async with session_factory() as session:
            await _record_provider_order(
                session,
                purchase_id=reservation.purchase_id,
                order=final_order,
                status=final_order.status,
            )
            await refund_provider_purchase(
                session,
                purchase_id=reservation.purchase_id,
                reason=f"provider_order_{final_order.status}",
            )
        raise ExternalOrderRejected(f"Provider order status: {final_order.status}")

    async with session_factory() as session:
        await _record_provider_order(
            session,
            purchase_id=reservation.purchase_id,
            order=final_order,
            status="pending_delivery",
        )
    raise ExternalOrderPending(
        purchase_id=reservation.purchase_id,
        purchase_code=reservation.purchase_code,
        provider_order_id=final_order.order_id,
        status=final_order.status,
    )


async def purchase_prodseller_product(
    session_factory: async_sessionmaker[AsyncSession],
    client: ProdSellerClient,
    *,
    telegram_id: int,
    product_id: int,
    allow_below_cost: bool,
    poll_attempts: int,
    poll_delay_seconds: float,
) -> PurchaseResult:
    """Backward-compatible wrapper for the original single-provider release."""

    return await purchase_provider_product(
        session_factory,
        client,
        provider_code=PROVIDER_CODE,
        telegram_id=telegram_id,
        product_id=product_id,
        allow_below_cost=allow_below_cost,
        poll_attempts=poll_attempts,
        poll_delay_seconds=poll_delay_seconds,
    )


async def retry_provider_purchase(
    session_factory: async_sessionmaker[AsyncSession],
    client: ProdSellerClient,
    *,
    purchase_id: int,
) -> PurchaseResult | None:
    async with session_factory() as session:
        purchase = await session.get(ProviderPurchase, purchase_id)
        if purchase is None:
            raise ExternalOrderRejected("Purchase not found")
        if purchase.status == "delivered" and purchase.order_id is not None:
            order = await session.get(Order, purchase.order_id)
            stock = await session.get(StockItem, order.stock_item_id) if order else None
            user = await session.get(User, purchase.user_id)
            if order is None or stock is None or user is None:
                raise ExternalOrderRejected("Delivered purchase is incomplete")
            return PurchaseResult(
                order_code=order.order_code,
                product_name=order.product_name,
                price=Decimal(order.price),
                stock_payload=stock.payload,
                new_balance=Decimal(user.balance),
                quantity=max(1, int(order.quantity or purchase.quantity or 1)),
                order_codes=(order.order_code,),
                instructions=order.instructions_snapshot,
                instructions_entities=order.instructions_entities_snapshot,
            )
        provider_order_id = purchase.provider_order_id
        if not provider_order_id:
            raise ExternalOrderManualReview(
                purchase_id=purchase.id,
                purchase_code=purchase.purchase_code,
                reason="No provider order ID is available; do not repeat the POST automatically.",
            )

    order = await client.get_order(provider_order_id)
    if order.delivered:
        async with session_factory() as session:
            return await _finalize_delivery(session, purchase_id=purchase_id, order=order)
    if order.status in {"failed", "cancelled", "refunded"}:
        async with session_factory() as session:
            await refund_provider_purchase(
                session,
                purchase_id=purchase_id,
                reason=f"provider_order_{order.status}",
            )
        return None
    async with session_factory() as session:
        await _record_provider_order(
            session,
            purchase_id=purchase_id,
            order=order,
            status="pending_delivery",
        )
    return None
