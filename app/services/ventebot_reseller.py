from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlparse

import httpx

from app.services.prodseller import (
    ProdSellerAmbiguousOrderError,
    ProdSellerAPIError,
    ProdSellerAuthenticationError,
    ProdSellerBadRequestError,
    ProdSellerBalance,
    ProdSellerClient,
    ProdSellerConfigurationError,
    ProdSellerInsufficientBalanceError,
    ProdSellerNotFoundError,
    ProdSellerOrder,
    ProdSellerOutOfStockError,
    ProdSellerProduct,
    ProdSellerRateLimitError,
    ProdSellerServerError,
    ProdSellerTransportError,
    RateLimitSnapshot,
)

logger = logging.getLogger(__name__)

ADAPTER_CODE = "ventebot_reseller_v1"
DEFAULT_BASE_URL = "https://ventetelegrambotrailway-production.up.railway.app"
_API_PATH = "/api/reseller"
_CENTS = Decimal("0.01")
_FAILED_STATUSES = {"failed", "cancelled", "canceled", "refunded", "rejected"}


def normalize_ventebot_base_url(value: str) -> str:
    normalized = value.strip().rstrip("/")
    lower = normalized.lower()
    for suffix in (
        "/api/reseller/openapi.json",
        "/api/swagger",
        "/api/reseller",
    ):
        if lower.endswith(suffix):
            normalized = normalized[: -len(suffix)].rstrip("/")
            break
    return normalized


def _decimal(value: Any, *, default: Decimal = Decimal("0")) -> Decimal:
    if value is None or value == "":
        return default
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ProdSellerAPIError(f"VenteBot returned an invalid numeric value: {value!r}") from exc


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ProdSellerAPIError(f"VenteBot returned an invalid integer value: {value!r}") from exc


def _absolute_image_url(value: Any, *, public_base: str) -> str | None:
    candidate = str(value or "").strip()
    if not candidate:
        return None
    parsed = urlparse(candidate)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return candidate
    if candidate.startswith("/"):
        return f"{public_base}{candidate}"
    return None


def _requires_activation_identifier(raw: dict[str, Any], delivery_type: str) -> bool:
    explicit = raw.get("requires_activation_identifier")
    if explicit is None:
        explicit = raw.get("requiresActivationIdentifier")
    if explicit is not None:
        return bool(explicit)
    normalized = delivery_type.replace("-", "_").lower()
    return "activation" in normalized or "identifier" in normalized


def _parse_product(raw: Any, *, public_base: str) -> ProdSellerProduct:
    if not isinstance(raw, dict):
        raise ProdSellerAPIError(
            "VenteBot returned an invalid product object",
            response_data=raw,
        )
    product_id = str(raw.get("id") or "").strip()
    name = str(raw.get("name") or "").strip()
    if not product_id or not name:
        raise ProdSellerAPIError(
            "VenteBot product is missing id or name",
            response_data=raw,
        )

    stock = _optional_int(raw.get("stock"))
    delivery_type = str(raw.get("delivery_type") or "instant").strip().lower() or "instant"
    tiers = raw.get("price_tiers")
    sold = _optional_int(raw.get("sold")) or 0
    return ProdSellerProduct(
        id=product_id,
        name=name,
        description=str(raw.get("description") or "").strip(),
        price=_decimal(raw.get("price_usd")).quantize(_CENTS),
        image_url=_absolute_image_url(raw.get("image_url"), public_base=public_base),
        delivery_type=delivery_type,
        sold=max(0, sold),
        in_stock=stock is None or stock > 0,
        stock=max(0, stock) if stock is not None else None,
        raw={**raw, "price_tiers": tiers if isinstance(tiers, list) else []},
        requires_activation_identifier=_requires_activation_identifier(raw, delivery_type),
    )


def _unwrap_dict(raw: Any, key: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ProdSellerAPIError(
            f"VenteBot returned an invalid {key} response",
            response_data=raw,
        )
    nested = raw.get(key)
    if isinstance(nested, dict):
        return nested
    return raw


def _parse_balance(raw: Any) -> ProdSellerBalance:
    data = _unwrap_dict(raw, "account")
    telegram_id = _optional_int(data.get("user_telegram_id"))
    username = str(data.get("username") or "").strip() or None
    key_name = str(data.get("key_name") or "").strip()
    key_prefix = str(data.get("key_prefix") or "").strip()
    membership = key_name or key_prefix or "reseller"
    return ProdSellerBalance(
        telegram_id=telegram_id,
        username=username,
        balance=_decimal(data.get("wallet_balance")).quantize(_CENTS),
        membership=membership,
        raw=dict(raw) if isinstance(raw, dict) else data,
        currency="USDT",
    )


def _stringify_account_data(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        lines: list[str] = []
        for key, item in value.items():
            if item is None or item == "":
                continue
            if isinstance(item, (dict, list)):
                rendered = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
            else:
                rendered = str(item).strip()
            if rendered:
                lines.append(f"{key}: {rendered}")
        return "\n".join(lines)
    if value is None:
        return ""
    return str(value).strip()


def _parse_order(raw: Any) -> ProdSellerOrder:
    order = _unwrap_dict(raw, "order")
    order_id = str(order.get("id") or order.get("order_id") or "").strip()
    if not order_id:
        raise ProdSellerAPIError(
            "VenteBot order response is missing id",
            response_data=raw,
        )

    delivered: list[str] = []
    items = order.get("items")
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            payload = _stringify_account_data(item.get("account_data"))
            if payload and payload not in delivered:
                delivered.append(payload)

    remote_status = str(order.get("status") or "pending").strip().lower() or "pending"
    if delivered and remote_status not in _FAILED_STATUSES:
        status = "delivered"
    elif remote_status in _FAILED_STATUSES:
        status = "cancelled" if remote_status == "canceled" else remote_status
    else:
        status = remote_status

    return ProdSellerOrder(
        order_id=order_id,
        status=status,
        product_id=str(order.get("product_id") or "").strip() or None,
        product_name=str(order.get("product_name") or "Producto API").strip() or "Producto API",
        quantity=max(1, _optional_int(order.get("quantity")) or 1),
        amount=_decimal(order.get("amount_usd")).quantize(_CENTS),
        discount_percent=Decimal("0.00"),
        discount_amount=Decimal("0.00"),
        delivered_keys=tuple(delivered),
        created_at=(str(order.get("created_at")).strip() if order.get("created_at") else None),
        raw=dict(raw) if isinstance(raw, dict) else order,
    )


def _quote_amount(raw: Any, *, quantity: int) -> Decimal:
    quote = _unwrap_dict(raw, "quote")
    for key in (
        "amount_usd",
        "total_usd",
        "total_price_usd",
        "total_amount_usd",
        "total",
        "amount",
    ):
        if quote.get(key) is not None:
            amount = _decimal(quote[key]).quantize(_CENTS)
            if amount <= 0:
                break
            return amount
    for key in ("unit_price_usd", "price_usd", "unit_price"):
        if quote.get(key) is not None:
            amount = (_decimal(quote[key]) * max(1, quantity)).quantize(_CENTS)
            if amount > 0:
                return amount
    raise ProdSellerAPIError(
        "VenteBot quote does not contain a valid USD total",
        response_data=raw,
    )


class VenteBotResellerClient(ProdSellerClient):
    """Adapter for the VenteBot Reseller API 1.2.0."""

    adapter_code = ADAPTER_CODE
    supports_order_status = True

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        allow_insecure_http: bool,
        timeout_seconds: float = 20.0,
        cache_seconds: int = 60,
        api_key_header: str = "X-Reseller-Key",
        provider_name: str = "VenteBot",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        api_key = api_key.strip()
        header = api_key_header.strip() or "X-Reseller-Key"
        provider_name = provider_name.strip() or "VenteBot"
        public_base = normalize_ventebot_base_url(base_url)
        parsed = urlparse(public_base)
        if not api_key:
            raise ProdSellerConfigurationError(f"{provider_name} API key is empty")
        if not header or any(character.isspace() for character in header):
            raise ProdSellerConfigurationError("VenteBot API key header is invalid")
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ProdSellerConfigurationError("VenteBot base URL is invalid")
        if parsed.scheme == "http" and not allow_insecure_http:
            raise ProdSellerConfigurationError(
                "Plain HTTP is disabled. Use the HTTPS VenteBot URL."
            )
        if parsed.scheme == "http":
            logger.warning(
                "%s is configured over plain HTTP; API keys and deliveries are not encrypted",
                provider_name,
            )

        self.base_url = public_base
        self.api_root = f"{public_base}{_API_PATH}"
        self.provider_name = provider_name
        self.cache_seconds = max(0, cache_seconds)
        self._client = httpx.AsyncClient(
            headers={
                header: api_key,
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "Shop-Ultra-Bot/VenteBot-Reseller-v1",
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
            try:
                return int(headers[name]) if headers.get(name) else None
            except ValueError:
                return None

        limit = integer("X-RateLimit-Limit")
        remaining = integer("X-RateLimit-Remaining")
        reset = headers.get("X-RateLimit-Reset")
        if limit is not None or remaining is not None or reset is not None:
            self._rate_limit = RateLimitSnapshot(limit=limit, remaining=remaining, reset=reset)

    @staticmethod
    def _error_details(data: Any, fallback: str) -> tuple[str, str]:
        if not isinstance(data, dict):
            return "", fallback
        return (
            str(data.get("code") or "").strip().lower(),
            str(data.get("message") or data.get("error") or fallback),
        )

    @classmethod
    def _raise_api_error(
        cls,
        status_code: int,
        data: Any,
        provider_name: str,
        *,
        order_creation: bool,
    ) -> None:
        code, message = cls._error_details(data, f"{provider_name} HTTP {status_code}")
        normalized = f"{code} {message}".lower()
        kwargs = {"status_code": status_code, "response_data": data}
        if status_code in {401, 403} or any(
            term in normalized for term in ("invalid_key", "invalid api key", "unauthorized")
        ):
            raise ProdSellerAuthenticationError(message, **kwargs)
        if status_code == 402 or "insufficient" in normalized and "balance" in normalized:
            raise ProdSellerInsufficientBalanceError(message, **kwargs)
        if (
            "out_of_stock" in normalized
            or "out of stock" in normalized
            or ("stock" in normalized and "insufficient" in normalized)
        ):
            raise ProdSellerOutOfStockError(message, **kwargs)
        if status_code == 404:
            raise ProdSellerNotFoundError(message, **kwargs)
        if status_code == 429:
            raise ProdSellerRateLimitError(message, **kwargs)
        if status_code >= 500:
            raise ProdSellerServerError(message, **kwargs)
        if status_code == 409 and order_creation:
            raise ProdSellerAmbiguousOrderError(message, **kwargs)
        if status_code in {400, 409, 422}:
            raise ProdSellerBadRequestError(message, **kwargs)
        raise ProdSellerAPIError(message, **kwargs)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        ambiguous_on_transport: bool = False,
    ) -> Any:
        url = f"{self.api_root}/{path.lstrip('/')}"
        try:
            response = await self._client.request(method, url, json=json_body)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            message = f"Could not reach {self.provider_name}: {type(exc).__name__}: {exc}"
            if ambiguous_on_transport:
                raise ProdSellerAmbiguousOrderError(message) from exc
            raise ProdSellerTransportError(message) from exc

        self._update_rate_limit(response.headers)
        if 300 <= response.status_code < 400:
            raise ProdSellerAPIError(
                f"{self.provider_name} redirected the API request (HTTP {response.status_code})",
                status_code=response.status_code,
            )
        try:
            data: Any = response.json()
        except ValueError:
            data = {"message": response.text[:1000] or f"HTTP {response.status_code}"}

        if 200 <= response.status_code < 300 and not (
            isinstance(data, dict) and data.get("success") is False
        ):
            return data

        self._raise_api_error(
            response.status_code,
            data,
            self.provider_name,
            order_creation=method.upper() == "POST" and path.rstrip("/") == "/orders",
        )
        raise AssertionError("unreachable")

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
            raw_products: Any
            if isinstance(data, list):
                raw_products = data
            elif isinstance(data, dict):
                raw_products = data.get("products")
            else:
                raw_products = None
            if not isinstance(raw_products, list):
                raise ProdSellerAPIError(
                    "VenteBot response does not contain a products list",
                    response_data=data,
                )
            products = tuple(
                _parse_product(item, public_base=self.base_url) for item in raw_products
            )
            stamp = time.monotonic()
            self._products_cache = (stamp, products)
            for product in products:
                self._product_cache[product.id] = (stamp, product)
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
        for product in await self.list_products(force_refresh=force_refresh):
            if product.id == product_id:
                return product
        raise ProdSellerNotFoundError(f"Product not found: {product_id}", status_code=404)

    async def get_balance(self) -> ProdSellerBalance:
        return _parse_balance(await self._request("GET", "/me"))

    async def quote_order(
        self,
        product_id: str,
        *,
        quantity: int = 1,
        purchase_options: dict[str, Any] | None = None,
    ) -> Decimal | None:
        del purchase_options
        if not product_id.strip():
            raise ProdSellerBadRequestError("Product ID is empty")
        if quantity < 1 or quantity > 100:
            raise ProdSellerBadRequestError("Quantity must be between 1 and 100")
        data = await self._request(
            "POST",
            "/quote",
            json_body={"product_id": product_id, "quantity": quantity},
        )
        return _quote_amount(data, quantity=quantity)

    async def create_order(
        self,
        product_id: str,
        *,
        quantity: int = 1,
        purchase_options: dict[str, Any] | None = None,
    ) -> ProdSellerOrder:
        product_id = product_id.strip()
        if not product_id:
            raise ProdSellerBadRequestError("Product ID is empty")
        if quantity < 1 or quantity > 100:
            raise ProdSellerBadRequestError("Quantity must be between 1 and 100")
        options = dict(purchase_options or {})
        idempotency_key = str(
            options.get("_idempotency_key")
            or options.get("idempotency_key")
            or f"shop-{secrets.token_urlsafe(16)}"
        ).strip()
        body: dict[str, Any] = {
            "product_id": product_id,
            "quantity": quantity,
            "idempotency_key": idempotency_key,
        }
        activation_identifier = str(options.get("activation_identifier") or "").strip()
        if activation_identifier:
            body["activation_identifier"] = activation_identifier
        customer_reference = str(
            options.get("_customer_reference") or options.get("customer_reference") or ""
        ).strip()
        if customer_reference:
            body["customer_reference"] = customer_reference

        data = await self._request(
            "POST",
            "/orders",
            json_body=body,
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

    async def submit_activation_identifier(
        self,
        order_id: str,
        activation_identifier: str,
    ) -> ProdSellerOrder:
        order_id = order_id.strip()
        activation_identifier = activation_identifier.strip()
        if not order_id or not activation_identifier:
            raise ProdSellerBadRequestError("Order ID and activation identifier are required")
        return _parse_order(
            await self._request(
                "POST",
                f"/orders/{order_id}/activation-identifier",
                json_body={"activation_identifier": activation_identifier},
                ambiguous_on_transport=True,
            )
        )
