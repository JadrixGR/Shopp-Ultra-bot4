from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

PROVIDER_CODE = "prodseller"


class ProdSellerError(Exception):
    """Base class for provider failures."""


class ProdSellerConfigurationError(ProdSellerError):
    pass


class ProdSellerAPIError(ProdSellerError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        response_data: Any = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_data = response_data


class ProdSellerAuthenticationError(ProdSellerAPIError):
    pass


class ProdSellerBadRequestError(ProdSellerAPIError):
    pass


class ProdSellerInsufficientBalanceError(ProdSellerAPIError):
    pass


class ProdSellerNotFoundError(ProdSellerAPIError):
    pass


class ProdSellerOutOfStockError(ProdSellerAPIError):
    pass


class ProdSellerRateLimitError(ProdSellerAPIError):
    pass


class ProdSellerServerError(ProdSellerAPIError):
    pass


class ProdSellerTransportError(ProdSellerAPIError):
    pass


class ProdSellerAmbiguousOrderError(ProdSellerTransportError):
    """The POST may have reached the provider but no response was received."""


@dataclass(frozen=True, slots=True)
class RateLimitSnapshot:
    limit: int | None = None
    remaining: int | None = None
    reset: str | None = None


@dataclass(frozen=True, slots=True)
class ProdSellerProduct:
    id: str
    name: str
    description: str
    price: Decimal
    image_url: str | None
    delivery_type: str
    sold: int
    in_stock: bool
    stock: int | None
    raw: dict[str, Any]
    requires_customer_email: bool = False
    requires_slot_months: bool = False
    slot_durations: tuple[int, ...] = ()
    quantity_fixed: int = 1
    slot_pricing_mode: str | None = None


@dataclass(frozen=True, slots=True)
class ProdSellerBalance:
    telegram_id: int | None
    username: str | None
    balance: Decimal
    membership: str
    raw: dict[str, Any]
    currency: str = "USDT"
    balance_text: str | None = None


@dataclass(frozen=True, slots=True)
class ProdSellerOrder:
    order_id: str
    status: str
    product_id: str | None
    product_name: str
    quantity: int
    amount: Decimal
    discount_percent: Decimal
    discount_amount: Decimal
    delivered_keys: tuple[str, ...]
    created_at: str | None
    raw: dict[str, Any]

    @property
    def delivered(self) -> bool:
        return self.status.lower() == "delivered" and bool(self.delivered_keys)

    @property
    def delivery_payload(self) -> str:
        return "\n".join(self.delivered_keys)


def _decimal(value: Any, *, default: Decimal = Decimal("0")) -> Decimal:
    if value is None or value == "":
        return default
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ProdSellerAPIError(f"Invalid numeric value returned by provider: {value!r}") from exc


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ProdSellerAPIError(f"Invalid integer value returned by provider: {value!r}") from exc


def _parse_product(raw: Any) -> ProdSellerProduct:
    if not isinstance(raw, dict):
        raise ProdSellerAPIError("Provider returned an invalid product object", response_data=raw)
    product_id = str(raw.get("id") or "").strip()
    name = str(raw.get("name") or "").strip()
    if not product_id or not name:
        raise ProdSellerAPIError("Provider product is missing id or name", response_data=raw)

    stock = _optional_int(raw.get("stock"))
    if "inStock" in raw:
        in_stock = bool(raw.get("inStock"))
    elif stock is not None:
        in_stock = stock > 0
    else:
        # Custom-delivery products may legitimately return stock=null.
        in_stock = True

    delivery_raw = raw.get("delivery")
    delivery_type = "instant"
    if isinstance(delivery_raw, dict):
        delivery_type = str(delivery_raw.get("type") or "instant").strip().lower()

    image_url_raw = str(raw.get("imageUrl") or "").strip()
    image_url: str | None = None
    if image_url_raw:
        parsed = urlparse(image_url_raw)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            image_url = image_url_raw

    return ProdSellerProduct(
        id=product_id,
        name=name,
        description=str(raw.get("description") or "").strip(),
        price=_decimal(raw.get("price")).quantize(Decimal("0.01")),
        image_url=image_url,
        delivery_type=delivery_type,
        sold=max(0, _optional_int(raw.get("sold")) or 0),
        in_stock=in_stock,
        stock=max(0, stock) if stock is not None else None,
        raw=dict(raw),
    )


def _parse_balance(raw: Any) -> ProdSellerBalance:
    if not isinstance(raw, dict):
        raise ProdSellerAPIError("Provider returned an invalid balance object", response_data=raw)
    telegram_id = _optional_int(raw.get("telegramId"))
    username_raw = str(raw.get("username") or "").strip()
    return ProdSellerBalance(
        telegram_id=telegram_id,
        username=username_raw or None,
        balance=_decimal(raw.get("balance")).quantize(Decimal("0.01")),
        membership=str(raw.get("membership") or "unknown").strip() or "unknown",
        raw=dict(raw),
    )


def _parse_order(raw: Any) -> ProdSellerOrder:
    if not isinstance(raw, dict):
        raise ProdSellerAPIError("Provider returned an invalid order object", response_data=raw)
    order_id = str(raw.get("orderId") or raw.get("id") or "").strip()
    if not order_id:
        raise ProdSellerAPIError("Provider order response is missing orderId", response_data=raw)

    product_raw = raw.get("product")
    product_id: str | None = None
    product_name = "Producto API"
    if isinstance(product_raw, dict):
        product_id_raw = str(product_raw.get("id") or "").strip()
        product_id = product_id_raw or None
        product_name = str(product_raw.get("name") or product_name).strip() or product_name

    delivered: list[str] = []
    single = raw.get("deliveredKey")
    if single is not None and str(single).strip():
        delivered.append(str(single).strip())
    many = raw.get("deliveredKeys")
    if isinstance(many, list):
        for item in many:
            value = str(item).strip()
            if value and value not in delivered:
                delivered.append(value)

    return ProdSellerOrder(
        order_id=order_id,
        status=str(raw.get("status") or "pending").strip().lower(),
        product_id=product_id,
        product_name=product_name,
        quantity=max(1, _optional_int(raw.get("quantity")) or 1),
        amount=_decimal(raw.get("amount")).quantize(Decimal("0.01")),
        discount_percent=_decimal(raw.get("discountPercent")).quantize(Decimal("0.01")),
        discount_amount=_decimal(raw.get("discountAmount")).quantize(Decimal("0.01")),
        delivered_keys=tuple(delivered),
        created_at=(str(raw.get("createdAt")).strip() if raw.get("createdAt") else None),
        raw=dict(raw),
    )


class ProdSellerClient:
    adapter_code = "prodseller_v1"
    supports_order_status = True

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        allow_insecure_http: bool,
        timeout_seconds: float = 20.0,
        cache_seconds: int = 60,
        api_key_header: str = "X-API-Key",
        provider_name: str = "ProdSeller",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        api_key = api_key.strip()
        api_key_header = api_key_header.strip()
        provider_name = provider_name.strip() or "Proveedor API"
        base_url = base_url.strip().rstrip("/")
        parsed = urlparse(base_url)
        if not api_key:
            raise ProdSellerConfigurationError(f"{provider_name} API key is empty")
        if not api_key_header or any(character.isspace() for character in api_key_header):
            raise ProdSellerConfigurationError("API key header is invalid")
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ProdSellerConfigurationError("ProdSeller base URL is invalid")
        if parsed.scheme == "http" and not allow_insecure_http:
            raise ProdSellerConfigurationError(
                "Plain HTTP is disabled. Enable PRODSELLER_ALLOW_INSECURE_HTTP only knowingly."
            )
        if parsed.scheme == "http":
            logger.warning(
                "%s is configured over plain HTTP; API keys and delivered products are not encrypted",
                provider_name,
            )

        self.base_url = base_url
        self.provider_name = provider_name
        self.cache_seconds = max(0, cache_seconds)
        self._client = httpx.AsyncClient(
            headers={
                api_key_header: api_key,
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "Shop-Ultra-Bot/Provider-v1",
            },
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=False,
            transport=transport,
        )
        self._cache_lock = asyncio.Lock()
        self._products_cache: tuple[float, tuple[ProdSellerProduct, ...]] | None = None
        self._product_cache: dict[str, tuple[float, ProdSellerProduct]] = {}
        self._rate_limit = RateLimitSnapshot()

    @property
    def rate_limit(self) -> RateLimitSnapshot:
        return self._rate_limit

    async def close(self) -> None:
        await self._client.aclose()

    def _update_rate_limit(self, headers: httpx.Headers) -> None:
        def integer(name: str) -> int | None:
            value = headers.get(name)
            if not value:
                return None
            try:
                return int(value)
            except ValueError:
                return None

        limit = integer("X-RateLimit-Limit")
        remaining = integer("X-RateLimit-Remaining")
        reset = headers.get("X-RateLimit-Reset")
        if limit is not None or remaining is not None or reset is not None:
            self._rate_limit = RateLimitSnapshot(limit=limit, remaining=remaining, reset=reset)

    @staticmethod
    def _error_message(data: Any, fallback: str) -> str:
        if isinstance(data, dict):
            value = data.get("error") or data.get("message")
            if value:
                return str(value)
        return fallback

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        ambiguous_on_transport: bool = False,
    ) -> Any:
        url = f"{self.base_url}/{path.lstrip('/')}"
        try:
            response = await self._client.request(method, url, json=json_body)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            message = f"Could not reach {self.provider_name}: {type(exc).__name__}: {exc}"
            if ambiguous_on_transport:
                raise ProdSellerAmbiguousOrderError(message) from exc
            raise ProdSellerTransportError(message) from exc

        self._update_rate_limit(response.headers)
        try:
            data: Any = response.json()
        except ValueError:
            data = {"error": response.text[:1000] or f"HTTP {response.status_code}"}

        if 200 <= response.status_code < 300:
            return data

        message = self._error_message(data, f"{self.provider_name} HTTP {response.status_code}")
        kwargs = {
            "status_code": response.status_code,
            "response_data": data,
        }
        if response.status_code == 400:
            raise ProdSellerBadRequestError(message, **kwargs)
        if response.status_code == 401:
            raise ProdSellerAuthenticationError(message, **kwargs)
        if response.status_code == 402:
            raise ProdSellerInsufficientBalanceError(message, **kwargs)
        if response.status_code == 404:
            raise ProdSellerNotFoundError(message, **kwargs)
        if response.status_code == 409:
            raise ProdSellerOutOfStockError(message, **kwargs)
        if response.status_code == 429:
            raise ProdSellerRateLimitError(message, **kwargs)
        if response.status_code >= 500:
            raise ProdSellerServerError(message, **kwargs)
        raise ProdSellerAPIError(message, **kwargs)

    async def list_products(self, *, force_refresh: bool = False) -> list[ProdSellerProduct]:
        now = time.monotonic()
        cached = self._products_cache
        if not force_refresh and cached is not None and now - cached[0] <= self.cache_seconds:
            return list(cached[1])

        async with self._cache_lock:
            now = time.monotonic()
            cached = self._products_cache
            if not force_refresh and cached is not None and now - cached[0] <= self.cache_seconds:
                return list(cached[1])

            data = await self._request("GET", "/products")
            if not isinstance(data, dict) or not isinstance(data.get("products"), list):
                raise ProdSellerAPIError(
                    "Provider response does not contain a products list",
                    response_data=data,
                )
            products = tuple(_parse_product(item) for item in data["products"])
            self._products_cache = (time.monotonic(), products)
            for product in products:
                self._product_cache[product.id] = (time.monotonic(), product)
            return list(products)

    async def get_product(
        self, product_id: str, *, force_refresh: bool = False
    ) -> ProdSellerProduct:
        product_id = product_id.strip()
        if not product_id:
            raise ProdSellerBadRequestError("Product ID is empty")
        now = time.monotonic()
        cached = self._product_cache.get(product_id)
        if not force_refresh and cached is not None and now - cached[0] <= self.cache_seconds:
            return cached[1]
        data = await self._request("GET", f"/products/{product_id}")
        product = _parse_product(data)
        self._product_cache[product.id] = (time.monotonic(), product)
        return product

    async def get_balance(self) -> ProdSellerBalance:
        return _parse_balance(await self._request("GET", "/balance"))

    async def create_order(
        self,
        product_id: str,
        *,
        quantity: int = 1,
        purchase_options: dict[str, Any] | None = None,
    ) -> ProdSellerOrder:
        del purchase_options
        if quantity < 1 or quantity > 100:
            raise ProdSellerBadRequestError("Quantity must be between 1 and 100")
        data = await self._request(
            "POST",
            "/orders",
            json_body={"productId": product_id, "quantity": quantity},
            ambiguous_on_transport=True,
        )
        order = _parse_order(data)
        self._products_cache = None
        self._product_cache.pop(product_id, None)
        return order

    async def get_order(self, order_id: str) -> ProdSellerOrder:
        order_id = order_id.strip()
        if not order_id:
            raise ProdSellerBadRequestError("Order ID is empty")
        return _parse_order(await self._request("GET", f"/orders/{order_id}"))

    @staticmethod
    def serialize_raw(raw: dict[str, Any]) -> str:
        return json.dumps(raw, ensure_ascii=False, separators=(",", ":"), default=str)

    async def wait_for_delivery(
        self,
        order: ProdSellerOrder,
        *,
        attempts: int,
        delay_seconds: float,
    ) -> ProdSellerOrder:
        current = order
        for attempt in range(max(1, attempts)):
            if current.delivered or current.status in {"failed", "cancelled", "refunded"}:
                return current
            if attempt + 1 >= attempts:
                break
            if delay_seconds > 0:
                await asyncio.sleep(delay_seconds)
            current = await self.get_order(current.order_id)
        return current


def parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None
