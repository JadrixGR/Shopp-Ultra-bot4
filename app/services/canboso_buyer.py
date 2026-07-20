from __future__ import annotations

import asyncio
import logging
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

ADAPTER_CODE = "canboso_buyer_v1"
_API_PATH = "/api/telegram-buyer"
_CENTS = Decimal("0.01")


def _decimal(value: Any, *, default: Decimal = Decimal("0")) -> Decimal:
    if value is None or value == "":
        return default
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ProdSellerAPIError(f"Canboso returned an invalid numeric value: {value!r}") from exc


def _optional_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    return _decimal(value)


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ProdSellerAPIError(f"Canboso returned an invalid integer value: {value!r}") from exc


def _currency(raw: dict[str, Any]) -> str:
    return str(raw.get("walletCurrency") or "USDT").strip().upper() or "USDT"


def _usd_value(
    raw: dict[str, Any],
    *,
    usd_keys: tuple[str, ...],
    native_keys: tuple[str, ...],
    required: bool,
    label: str,
) -> Decimal:
    for key in usd_keys:
        value = _optional_decimal(raw.get(key))
        if value is not None:
            return value.quantize(_CENTS)

    currency = _currency(raw)
    if currency in {"USD", "USDT"}:
        for key in native_keys:
            value = _optional_decimal(raw.get(key))
            if value is not None:
                return value.quantize(_CENTS)

    usd_rate = _optional_decimal(raw.get("usdRate"))
    if usd_rate is not None and usd_rate > 0:
        for key in native_keys:
            value = _optional_decimal(raw.get(key))
            if value is not None:
                return (value / usd_rate).quantize(_CENTS)

    if required:
        raise ProdSellerAPIError(
            f"Canboso did not return {label} in USD/USDT. "
            "The Shop Ultra wallet uses USDT and cannot safely infer the conversion."
        )
    return Decimal("0.00")


def _slot_durations(value: Any) -> tuple[int, ...]:
    if not isinstance(value, list):
        return ()
    result: list[int] = []
    for item in value:
        number = _optional_int(item)
        if number is not None and 1 <= number <= 120 and number not in result:
            result.append(number)
    return tuple(sorted(result))


def _image_url(raw: dict[str, Any]) -> str | None:
    value = str(raw.get("imageUrl") or raw.get("image_url") or raw.get("image") or "").strip()
    if not value:
        return None
    parsed = urlparse(value)
    return value if parsed.scheme in {"http", "https"} and parsed.netloc else None


def _parse_product(raw: Any) -> ProdSellerProduct:
    if not isinstance(raw, dict):
        raise ProdSellerAPIError("Canboso returned an invalid product object", response_data=raw)

    product_id = str(raw.get("_id") or raw.get("product_id") or raw.get("id") or "").strip()
    name = str(
        raw.get("product_name") or raw.get("product_name_raw") or raw.get("name") or ""
    ).strip()
    if not product_id or not name:
        raise ProdSellerAPIError(
            "Canboso product is missing _id/product_id or product_name",
            response_data=raw,
        )

    price = _usd_value(
        raw,
        usd_keys=("usdPricing", "priceUsd", "usd_price"),
        native_keys=("walletPricing", "pricing", "price"),
        required=True,
        label="the product price",
    )

    stats = raw.get("stats") if isinstance(raw.get("stats"), dict) else {}
    available = _optional_int(stats.get("available"))
    total = _optional_int(stats.get("total"))
    sold = max(0, _optional_int(stats.get("sold")) or 0)
    if available is None and total is not None:
        available = max(0, total - sold)

    is_slot = bool(raw.get("isSlotProduct")) or product_id == "slot_chatgpt_business"
    requires_email = bool(raw.get("requiresCustomerEmail")) or is_slot
    requires_months = bool(raw.get("requiresSlotMonths")) or is_slot
    durations = _slot_durations(raw.get("slotDurations"))
    if requires_months and not durations:
        durations = (1,)

    quantity_fixed = _optional_int(raw.get("quantityFixed")) or 1
    quantity_fixed = min(100, max(1, quantity_fixed))
    pricing_mode_raw = str(raw.get("slotPricingMode") or "").strip().lower()
    pricing_mode = pricing_mode_raw or None

    in_stock = available is None or available > 0
    return ProdSellerProduct(
        id=product_id,
        name=name,
        description=str(raw.get("description") or raw.get("description_raw") or "").strip(),
        price=price,
        image_url=_image_url(raw),
        delivery_type="instant",
        sold=sold,
        in_stock=in_stock,
        stock=max(0, available) if available is not None else None,
        raw=dict(raw),
        requires_customer_email=requires_email,
        requires_slot_months=requires_months,
        slot_durations=durations,
        quantity_fixed=quantity_fixed,
        slot_pricing_mode=pricing_mode,
    )


def _parse_balance(raw: Any) -> ProdSellerBalance:
    if not isinstance(raw, dict):
        raise ProdSellerAPIError("Canboso returned an invalid balance object", response_data=raw)
    if raw.get("success") is False:
        raise ProdSellerAuthenticationError(
            str(raw.get("message") or "Invalid API key"), response_data=raw
        )

    requester = raw.get("requester") if isinstance(raw.get("requester"), dict) else {}
    currency = _currency(raw)
    balance = _usd_value(
        raw,
        usd_keys=("balanceUsd", "usdtBalance"),
        native_keys=("balance", "balanceVnd"),
        required=False,
        label="the wallet balance",
    )
    native_text = str(raw.get("balanceText") or "").strip() or None
    bot_source = str(raw.get("botSource") or "buyer").strip() or "buyer"
    username = str(requester.get("name") or "").strip() or None
    telegram_id = _optional_int(requester.get("chatId"))
    return ProdSellerBalance(
        telegram_id=telegram_id,
        username=username,
        balance=balance,
        membership=f"{bot_source} · {currency}",
        raw=dict(raw),
        currency=currency,
        balance_text=native_text,
    )


def _account_payload(accounts: Any) -> list[str]:
    if not isinstance(accounts, list):
        return []
    rendered: list[str] = []
    for index, account in enumerate(accounts, start=1):
        if isinstance(account, str):
            value = account.strip()
            if value:
                rendered.append(value)
            continue
        if not isinstance(account, dict):
            continue
        lines = [f"Cuenta {index}"]
        fields = (
            ("Usuario", "user"),
            ("Contraseña", "password"),
            ("Correo de verificación", "verifyEmail"),
            ("ID del producto", "productItemId"),
            ("Entregado", "deliveredAt"),
        )
        for label, key in fields:
            value = account.get(key)
            if value is not None and str(value).strip():
                lines.append(f"{label}: {str(value).strip()}")
        if len(lines) > 1:
            rendered.append("\n".join(lines))
    return rendered


def _parse_order(raw: Any) -> ProdSellerOrder:
    if not isinstance(raw, dict):
        raise ProdSellerAPIError("Canboso returned an invalid purchase object", response_data=raw)
    if raw.get("success") is False:
        raise ProdSellerBadRequestError(
            str(raw.get("message") or "Purchase rejected"), response_data=raw
        )

    order_id = str(raw.get("orderCode") or raw.get("orderId") or raw.get("id") or "").strip()
    if not order_id:
        raise ProdSellerAPIError(
            "Canboso purchase response is missing orderCode", response_data=raw
        )

    delivered = _account_payload(raw.get("deliveredAccounts"))

    single = raw.get("deliveredKey")
    if single is not None and str(single).strip():
        delivered.append(str(single).strip())
    many = raw.get("deliveredKeys")
    if isinstance(many, list):
        for item in many:
            value = str(item).strip()
            if value and value not in delivered:
                delivered.append(value)

    workspace_status = str(raw.get("workspaceInviteStatus") or "").strip()
    customer_email = str(raw.get("customerEmail") or "").strip()
    owner_email = str(raw.get("workspaceOwnerEmail") or "").strip()
    invite_error = str(raw.get("inviteError") or "").strip()
    if workspace_status or customer_email or owner_email:
        lines = ["ChatGPT Business Slot"]
        if customer_email:
            lines.append(f"Correo del cliente: {customer_email}")
        if raw.get("slotMonths") is not None:
            lines.append(f"Duración: {raw.get('slotMonths')} mes(es)")
        if workspace_status:
            lines.append(f"Estado de invitación: {workspace_status}")
        if owner_email:
            lines.append(f"Correo del workspace: {owner_email}")
        if invite_error:
            lines.append(f"Observación: {invite_error}")
        delivered.append("\n".join(lines))

    status = "delivered" if delivered and not invite_error else "pending"
    quantity = _optional_int(raw.get("finalQuantity")) or _optional_int(raw.get("quantity")) or 1
    amount = _usd_value(
        raw,
        usd_keys=("amountUsd",),
        native_keys=("amount",),
        required=False,
        label="the order amount",
    )
    discount_amount = _usd_value(
        raw,
        usd_keys=("discountAmountUsd",),
        native_keys=("discountAmount",),
        required=False,
        label="the discount amount",
    )

    return ProdSellerOrder(
        order_id=order_id,
        status=status,
        product_id=None,
        product_name=str(
            raw.get("productType") or raw.get("productTypeRaw") or "Producto API"
        ).strip()
        or "Producto API",
        quantity=max(1, quantity),
        amount=amount,
        discount_percent=_decimal(raw.get("discountPercent")).quantize(_CENTS),
        discount_amount=discount_amount,
        delivered_keys=tuple(delivered),
        created_at=(str(raw.get("createdAt")).strip() if raw.get("createdAt") else None),
        raw=dict(raw),
    )


class CanbosoBuyerClient(ProdSellerClient):
    """Adapter for Canboso Buyer API 1.2.0.

    Authentication is sent as the ``key`` query parameter for reads and as the
    ``key`` field in the purchase JSON body, exactly as documented by the API.
    """

    adapter_code = ADAPTER_CODE
    supports_order_status = False

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        allow_insecure_http: bool,
        timeout_seconds: float = 20.0,
        cache_seconds: int = 60,
        api_key_header: str = "X-API-Key",
        provider_name: str = "Canboso",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        del api_key_header  # This API authenticates through the `key` parameter/body field.
        api_key = api_key.strip()
        provider_name = provider_name.strip() or "Canboso"
        configured_url = base_url.strip().rstrip("/")
        parsed = urlparse(configured_url)
        if not api_key:
            raise ProdSellerConfigurationError(f"{provider_name} API key is empty")
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ProdSellerConfigurationError("Canboso base URL is invalid")
        if parsed.scheme == "http" and not allow_insecure_http:
            raise ProdSellerConfigurationError(
                "Plain HTTP is disabled. Use https://canboso.com or explicitly allow HTTP."
            )
        if parsed.scheme == "http":
            logger.warning(
                "%s is configured over plain HTTP; API keys and delivered accounts are not encrypted",
                provider_name,
            )

        if configured_url.lower().endswith(_API_PATH):
            api_root = configured_url
            public_base = configured_url[: -len(_API_PATH)] or configured_url
        else:
            api_root = f"{configured_url}{_API_PATH}"
            public_base = configured_url

        self.base_url = public_base
        self.api_root = api_root
        self.provider_name = provider_name
        self.cache_seconds = max(0, cache_seconds)
        self._api_key = api_key
        self._client = httpx.AsyncClient(
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "Shop-Ultra-Bot/Canboso-Buyer-v1",
            },
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=False,
            transport=transport,
        )
        self._cache_lock = asyncio.Lock()
        self._products_cache: tuple[float, tuple[ProdSellerProduct, ...]] | None = None
        self._product_cache: dict[str, tuple[float, ProdSellerProduct]] = {}
        self._rate_limit = RateLimitSnapshot()

    async def close(self) -> None:
        await self._client.aclose()

    @property
    def rate_limit(self) -> RateLimitSnapshot:
        return self._rate_limit

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
    def _message(data: Any, fallback: str) -> str:
        if isinstance(data, dict):
            value = data.get("message") or data.get("error")
            if value:
                return str(value)
        return fallback

    @staticmethod
    def _raise_api_error(status_code: int, data: Any, provider_name: str) -> None:
        message = CanbosoBuyerClient._message(data, f"{provider_name} HTTP {status_code}")
        normalized = message.lower()
        kwargs = {"status_code": status_code, "response_data": data}
        if status_code == 401 or "invalid api key" in normalized or "api key" in normalized:
            raise ProdSellerAuthenticationError(message, **kwargs)
        if status_code == 402 or (
            "balance" in normalized
            and any(term in normalized for term in ("not enough", "insufficient", "enough"))
        ):
            raise ProdSellerInsufficientBalanceError(message, **kwargs)
        if status_code == 409 or any(term in normalized for term in ("inventory", "out of stock")):
            raise ProdSellerOutOfStockError(message, **kwargs)
        if status_code == 404:
            raise ProdSellerNotFoundError(message, **kwargs)
        if status_code == 429:
            raise ProdSellerRateLimitError(message, **kwargs)
        if status_code >= 500:
            raise ProdSellerServerError(message, **kwargs)
        if status_code == 400:
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
        params = {"key": self._api_key} if method.upper() == "GET" else None
        try:
            response = await self._client.request(method, url, params=params, json=json_body)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            message = f"Could not reach {self.provider_name}: {type(exc).__name__}: {exc}"
            if ambiguous_on_transport:
                raise ProdSellerAmbiguousOrderError(message) from exc
            raise ProdSellerTransportError(message) from exc

        self._update_rate_limit(response.headers)
        try:
            data: Any = response.json()
        except ValueError:
            data = {"message": response.text[:1000] or f"HTTP {response.status_code}"}

        if 200 <= response.status_code < 300 and not (
            isinstance(data, dict) and data.get("success") is False
        ):
            return data

        self._raise_api_error(response.status_code, data, self.provider_name)
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
            if not isinstance(data, dict) or not isinstance(data.get("products"), list):
                raise ProdSellerAPIError(
                    "Canboso response does not contain a products list",
                    response_data=data,
                )
            products = tuple(_parse_product(item) for item in data["products"])
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
        products = await self.list_products(force_refresh=force_refresh)
        for product in products:
            if product.id == product_id:
                return product
        raise ProdSellerNotFoundError(f"Product not found: {product_id}", status_code=404)

    async def get_balance(self) -> ProdSellerBalance:
        return _parse_balance(await self._request("GET", "/balance"))

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

        body: dict[str, Any] = {
            "key": self._api_key,
            "product_id": product_id,
            "quantity": quantity,
        }
        options = dict(purchase_options or {})
        customer_email = str(options.get("customer_email") or "").strip()
        if customer_email:
            body["customer_email"] = customer_email
        if options.get("slot_months") is not None:
            try:
                body["slot_months"] = int(options["slot_months"])
            except (TypeError, ValueError) as exc:
                raise ProdSellerBadRequestError("slot_months must be an integer") from exc

        data = await self._request(
            "POST",
            "/purchase",
            json_body=body,
            ambiguous_on_transport=True,
        )
        order = _parse_order(data)
        self._products_cache = None
        self._product_cache.pop(product_id, None)
        return order

    async def get_order(self, order_id: str) -> ProdSellerOrder:
        del order_id
        raise ProdSellerBadRequestError(
            "Canboso Buyer API 1.2.0 does not expose an order-status endpoint. "
            "Review the order in the provider panel before refunding it."
        )

    async def wait_for_delivery(
        self,
        order: ProdSellerOrder,
        *,
        attempts: int,
        delay_seconds: float,
    ) -> ProdSellerOrder:
        del attempts, delay_seconds
        return order
