from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    first_name: Mapped[str] = mapped_column(String(128), default="")
    language: Mapped[str] = mapped_column(String(5), default="es")
    balance: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), default=Decimal("0.00"), nullable=False
    )
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    orders: Mapped[list[Order]] = relationship(back_populates="user")
    deposits: Mapped[list[Deposit]] = relationship(back_populates="user")
    provider_purchases: Mapped[list[ProviderPurchase]] = relationship(back_populates="user")
    refunds: Mapped[list[Refund]] = relationship(back_populates="user")
    balance_adjustments: Mapped[list[BalanceAdjustment]] = relationship(back_populates="user")


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint(
            "provider_code",
            "external_product_id",
            name="uq_product_provider_external_id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(180), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    description_entities: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    instructions: Mapped[str] = mapped_column(Text, default="", nullable=False)
    instructions_entities: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    button_emoji: Mapped[str] = mapped_column(String(32), default="🛍️", nullable=False)
    button_style: Mapped[str] = mapped_column(String(16), default="primary", nullable=False)
    media_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    media_file_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Optional service duration used by the admin prorated-refund calculator.
    # Existing products remain NULL and the refund workflow will ask for the duration.
    service_days: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # External fulfillment metadata. Existing products remain local because all
    # of these fields default to NULL.
    provider_code: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    external_product_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    provider_cost: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    provider_stock: Mapped[int | None] = mapped_column(Integer, nullable=True)
    provider_in_stock: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    provider_image_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # JSON with provider-owned purchase requirements. It stores only catalog
    # metadata (for example required email and allowed slot durations), never a
    # customer's submitted email or delivered credentials.
    provider_metadata: Mapped[str | None] = mapped_column(Text, nullable=True)
    # API synchronization never overwrites a retail price. The flag documents
    # that the store owner controls the public price independently of provider cost.
    provider_price_locked: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    provider_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    stock_items: Mapped[list[StockItem]] = relationship(back_populates="product")
    orders: Mapped[list[Order]] = relationship(back_populates="product")
    provider_purchases: Mapped[list[ProviderPurchase]] = relationship(back_populates="product")

    @property
    def is_external(self) -> bool:
        return bool(self.provider_code and self.external_product_id)


class StockItem(Base):
    __tablename__ = "stock_items"
    __table_args__ = (UniqueConstraint("product_id", "payload_hash", name="uq_stock_product_hash"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), index=True
    )
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="available", nullable=False, index=True)
    sold_to_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    sold_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    product: Mapped[Product] = relationship(back_populates="stock_items")


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    stock_item_id: Mapped[int] = mapped_column(ForeignKey("stock_items.id"), unique=True)
    product_name: Mapped[str] = mapped_column(String(180), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    instructions_snapshot: Mapped[str] = mapped_column(Text, default="", nullable=False)
    instructions_entities_snapshot: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="completed", nullable=False)
    refunded_amount: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), default=Decimal("0.00"), nullable=False
    )
    refund_status: Mapped[str] = mapped_column(String(16), default="none", nullable=False)

    provider_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    provider_order_id: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    provider_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    provider_discount_percent: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)
    provider_discount_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    delivered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    user: Mapped[User] = relationship(back_populates="orders")
    product: Mapped[Product] = relationship(back_populates="orders")
    refunds: Mapped[list[Refund]] = relationship(back_populates="order")


class ProviderPurchase(Base):
    """Tracks the non-atomic part of an external API purchase.

    The local balance is reserved before the remote POST request. Explicit API
    errors are refunded automatically. Ambiguous network failures remain in
    manual_review so an administrator can verify the provider panel without
    accidentally creating a duplicate order.
    """

    __tablename__ = "provider_purchases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    purchase_code: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    order_id: Mapped[int | None] = mapped_column(
        ForeignKey("orders.id", ondelete="SET NULL"), unique=True, nullable=True
    )
    provider_code: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    provider_product_id: Mapped[str] = mapped_column(String(128), nullable=False)
    provider_order_id: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    quantity: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    local_price: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    expected_provider_cost: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    actual_provider_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    status: Mapped[str] = mapped_column(
        String(24), default="processing", nullable=False, index=True
    )
    delivery_payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # JSON describing the parameters submitted to the provider. This is useful
    # for manual review after an ambiguous network failure.
    request_payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship(back_populates="provider_purchases")
    product: Mapped[Product] = relationship(back_populates="provider_purchases")


class Refund(Base):
    __tablename__ = "refunds"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    refund_code: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    admin_telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    refund_type: Mapped[str] = mapped_column(String(20), nullable=False)
    original_price: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    total_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    used_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    remaining_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    order: Mapped[Order] = relationship(back_populates="refunds")
    user: Mapped[User] = relationship(back_populates="refunds")


class BalanceAdjustment(Base):
    __tablename__ = "balance_adjustments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    adjustment_code: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    admin_telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    balance_before: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    balance_after: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    adjustment_type: Mapped[str] = mapped_column(String(24), nullable=False)
    reference_type: Mapped[str | None] = mapped_column(String(24), nullable=True)
    reference_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    user: Mapped[User] = relationship(back_populates="balance_adjustments")


class Broadcast(Base):
    __tablename__ = "broadcasts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    broadcast_code: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    admin_telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(24), default="announcement", nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False, index=True)
    attempted: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sent: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    blocked: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Deposit(Base):
    __tablename__ = "deposits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    requested_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    credited_amount: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), default=Decimal("0.00"), nullable=False
    )
    bonus_amount: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), default=Decimal("0.00"), nullable=False
    )
    currency: Mapped[str] = mapped_column(String(12), default="USDT", nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False, index=True)
    claimed_transaction_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    transaction_id: Mapped[str | None] = mapped_column(
        String(160), unique=True, nullable=True, index=True
    )
    verify_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_verify_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship(back_populates="deposits")


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )
