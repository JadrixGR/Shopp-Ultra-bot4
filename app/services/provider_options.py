from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from app.models import Product
from app.services.prodseller import ProdSellerBadRequestError, ProdSellerProduct

_CENTS = Decimal("0.01")
_EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_MAX_PROVIDER_QUANTITY = 100


@dataclass(frozen=True, slots=True)
class ProviderProductOptions:
    requires_customer_email: bool = False
    requires_slot_months: bool = False
    slot_durations: tuple[int, ...] = ()
    quantity_fixed: int = 1
    slot_pricing_mode: str | None = None

    @classmethod
    def from_remote(cls, remote: ProdSellerProduct) -> ProviderProductOptions:
        durations = tuple(
            sorted(
                {
                    int(value)
                    for value in remote.slot_durations
                    if isinstance(value, int) and 1 <= value <= 120
                }
            )
        )
        requires_months = bool(remote.requires_slot_months)
        if requires_months and not durations:
            durations = (1,)
        return cls(
            requires_customer_email=bool(remote.requires_customer_email),
            requires_slot_months=requires_months,
            slot_durations=durations,
            quantity_fixed=max(1, min(100, int(remote.quantity_fixed or 1))),
            slot_pricing_mode=(
                str(remote.slot_pricing_mode).strip().lower() if remote.slot_pricing_mode else None
            ),
        )

    @property
    def requires_input(self) -> bool:
        return self.requires_customer_email or self.requires_slot_months

    @property
    def price_is_per_month(self) -> bool:
        return self.requires_slot_months and self.slot_pricing_mode in {
            "per_month",
            "monthly",
            "month",
        }

    @property
    def max_requested_quantity(self) -> int:
        return max(1, _MAX_PROVIDER_QUANTITY // max(1, self.quantity_fixed))

    def normalize_requested_quantity(self, value: Any) -> int:
        try:
            quantity = int(value)
        except (TypeError, ValueError) as exc:
            raise ProdSellerBadRequestError("La cantidad seleccionada no es válida") from exc
        if quantity < 1 or quantity > self.max_requested_quantity:
            raise ProdSellerBadRequestError(
                f"La cantidad debe estar entre 1 y {self.max_requested_quantity}"
            )
        return quantity

    def provider_quantity(self, requested_quantity: Any) -> int:
        return self.quantity_fixed * self.normalize_requested_quantity(requested_quantity)

    def validate_months(self, value: Any) -> int | None:
        if not self.requires_slot_months:
            return None
        try:
            months = int(value)
        except (TypeError, ValueError) as exc:
            raise ProdSellerBadRequestError("Debes seleccionar una duración válida") from exc
        if self.slot_durations and months not in self.slot_durations:
            raise ProdSellerBadRequestError("La duración seleccionada no está disponible")
        if months < 1 or months > 120:
            raise ProdSellerBadRequestError("La duración seleccionada no es válida")
        return months

    def normalize_purchase_options(
        self,
        raw: dict[str, Any] | None,
    ) -> dict[str, Any]:
        source = dict(raw or {})
        result: dict[str, Any] = {}

        if self.requires_customer_email:
            email = str(source.get("customer_email") or "").strip()
            if not valid_customer_email(email):
                raise ProdSellerBadRequestError("Debes indicar un correo válido")
            result["customer_email"] = email

        months = self.validate_months(source.get("slot_months"))
        if months is not None:
            result["slot_months"] = months
        return result


def valid_customer_email(value: str) -> bool:
    candidate = value.strip()
    return bool(candidate and len(candidate) <= 254 and _EMAIL_PATTERN.fullmatch(candidate))


def serialize_provider_options(remote: ProdSellerProduct) -> str:
    options = ProviderProductOptions.from_remote(remote)
    payload = asdict(options)
    payload["slot_durations"] = list(options.slot_durations)
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def product_provider_options(product: Product) -> ProviderProductOptions:
    raw = product.provider_metadata
    if not raw:
        return ProviderProductOptions()
    try:
        data = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return ProviderProductOptions()
    if not isinstance(data, dict):
        return ProviderProductOptions()

    durations_raw = data.get("slot_durations")
    durations: tuple[int, ...] = ()
    if isinstance(durations_raw, list):
        parsed: set[int] = set()
        for value in durations_raw:
            try:
                number = int(value)
            except (TypeError, ValueError):
                continue
            if 1 <= number <= 120:
                parsed.add(number)
        durations = tuple(sorted(parsed))

    try:
        quantity_fixed = int(data.get("quantity_fixed") or 1)
    except (TypeError, ValueError):
        quantity_fixed = 1

    requires_months = bool(data.get("requires_slot_months", False))
    if requires_months and not durations:
        durations = (1,)
    pricing_mode = str(data.get("slot_pricing_mode") or "").strip().lower() or None
    return ProviderProductOptions(
        requires_customer_email=bool(data.get("requires_customer_email", False)),
        requires_slot_months=requires_months,
        slot_durations=durations,
        quantity_fixed=max(1, min(100, quantity_fixed)),
        slot_pricing_mode=pricing_mode,
    )


def base_provider_cost(remote: ProdSellerProduct) -> Decimal:
    quantity = max(1, int(remote.quantity_fixed or 1))
    return (Decimal(remote.price) * quantity).quantize(_CENTS, rounding=ROUND_HALF_UP)


def effective_provider_cost(
    remote: ProdSellerProduct,
    purchase_options: dict[str, Any] | None,
    *,
    requested_quantity: int = 1,
) -> Decimal:
    options = ProviderProductOptions.from_remote(remote)
    normalized = options.normalize_purchase_options(purchase_options)
    requested = options.normalize_requested_quantity(requested_quantity)
    amount = base_provider_cost(remote) * requested
    if options.price_is_per_month:
        amount *= Decimal(int(normalized["slot_months"]))
    return amount.quantize(_CENTS, rounding=ROUND_HALF_UP)


def effective_retail_price(
    base_price: Decimal,
    options: ProviderProductOptions,
    purchase_options: dict[str, Any] | None,
    *,
    requested_quantity: int = 1,
) -> Decimal:
    normalized = options.normalize_purchase_options(purchase_options)
    requested = options.normalize_requested_quantity(requested_quantity)
    amount = Decimal(base_price) * requested
    if options.price_is_per_month:
        amount *= Decimal(int(normalized["slot_months"]))
    return amount.quantize(_CENTS, rounding=ROUND_HALF_UP)
