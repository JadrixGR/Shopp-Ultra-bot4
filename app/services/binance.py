from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlencode

import httpx


class BinanceAPIError(RuntimeError):
    """Error returned while calling the signed Binance API."""

    def __init__(
        self,
        message: str,
        *,
        code: int | str | None = None,
        http_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status


class BinanceTransactionNotFound(LookupError):
    """The API call succeeded, but the claimed transaction was not in the result."""

    def __init__(
        self,
        *,
        inspected_count: int = 0,
        observed_transaction_ids: tuple[str, ...] = (),
        start_time_ms: int | None = None,
        end_time_ms: int | None = None,
    ) -> None:
        super().__init__("transaction_not_found")
        self.inspected_count = inspected_count
        self.observed_transaction_ids = observed_transaction_ids
        self.start_time_ms = start_time_ms
        self.end_time_ms = end_time_ms


class BinanceTransactionMismatch(ValueError):
    """A transaction with the claimed reference exists, but it is not valid."""

    def __init__(self, reason: str, *, transaction_id: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.transaction_id = transaction_id


# Binance commonly exposes M_P_123..., while its application may show only
# the numeric suffix as "Order ID". Separators are accepted because copied text
# can vary between mobile clients and languages.
_REFERENCE_RE = re.compile(r"(?i)(?:M[\s_-]*P[\s_-]*)?(\d{6,})")
_REFERENCE_FIELD_NAMES = frozenset(
    {
        "transactionid",
        "transaction_id",
        "orderid",
        "order_id",
        "tradeid",
        "trade_id",
        "bizid",
        "biz_id",
        "prepayid",
        "prepay_id",
        "merchanttradeno",
        "merchant_trade_no",
    }
)
_ACCEPTED_INCOME_ORDER_TYPES = frozenset({"C2C", "PAY", "C2B"})
_MAX_BINANCE_QUERY_SPAN = timedelta(days=90)
_PAYMENT_CLOCK_TOLERANCE = timedelta(minutes=10)


def extract_transaction_reference(raw: str) -> str:
    """Extract the client-visible Binance Order ID.

    Numeric references are always returned without ``M_P_`` so customers can
    paste only the number shown by Binance. Non-numeric reference formats are
    retained as an uppercase compatibility fallback.
    """

    text = str(raw or "").strip()
    match = _REFERENCE_RE.search(text)
    if match:
        return match.group(1)

    compact = "".join(text.split()).upper()
    if 6 <= len(compact) <= 160 and re.fullmatch(r"[A-Z0-9_-]+", compact):
        return compact
    raise ValueError("invalid_transaction_reference")


def transaction_reference_key(raw: str) -> str:
    """Return a prefix-independent comparison key for a transaction reference."""

    reference = extract_transaction_reference(raw)
    if reference.isdigit():
        return reference.lstrip("0") or "0"
    return reference.upper()


def transaction_reference_aliases(raw: str) -> frozenset[str]:
    reference = extract_transaction_reference(raw)
    if reference.isdigit():
        numeric = reference.lstrip("0") or "0"
        return frozenset({numeric, f"M_P_{numeric}"})
    return frozenset({reference.upper()})


def canonical_transaction_reference(raw: str) -> str:
    reference = extract_transaction_reference(raw)
    if reference.isdigit():
        return f"M_P_{reference.lstrip('0') or '0'}"
    return reference.upper()


def _normalize_field_name(value: object) -> str:
    return re.sub(r"[^a-z0-9_]", "", str(value).lower())


def _iter_reference_values(value: Any, *, depth: int = 0) -> Iterable[str]:
    """Yield transaction/order references from known response fields only."""

    if depth > 4:
        return
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized_key = _normalize_field_name(key)
            if normalized_key in _REFERENCE_FIELD_NAMES and nested is not None:
                yield str(nested)
            if isinstance(nested, (dict, list, tuple)):
                yield from _iter_reference_values(nested, depth=depth + 1)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            yield from _iter_reference_values(nested, depth=depth + 1)


def transaction_reference_candidates(transaction: dict[str, Any]) -> frozenset[str]:
    candidates: set[str] = set()
    for raw in _iter_reference_values(transaction):
        try:
            candidates.add(transaction_reference_key(raw))
        except ValueError:
            continue
    return frozenset(candidates)


def display_transaction_reference(raw: str) -> str:
    """Return the number customers see, falling back to the original reference."""

    try:
        return extract_transaction_reference(raw)
    except ValueError:
        return str(raw).strip()


@dataclass(frozen=True, slots=True)
class VerifiedPayTransaction:
    transaction_id: str
    customer_order_id: str
    amount: Decimal
    currency: str
    transaction_time_ms: int
    raw: dict[str, Any]


@dataclass(frozen=True, slots=True)
class BinanceHistoryDiagnostic:
    transaction_count: int
    incoming_usdt_count: int
    latest_transaction_time_ms: int | None
    recent_order_ids: tuple[str, ...]
    start_time_ms: int
    end_time_ms: int


class BinancePayHistoryClient:
    """Read-only client for Binance Pay trade history.

    The authenticated endpoint is account-scoped. Incoming transactions are
    recognized by a positive top-level amount. ``receiverInfo.binanceId`` is a
    Binance UID, not necessarily the Pay ID shown to customers, so the Pay ID is
    compared strictly only when the API actually returns ``accountId`` or an
    exact matching receiver identifier.
    """

    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        base_url: str = "https://api.binance.com",
        history_hours: int = 72,
        cache_seconds: int = 15,
        recv_window_ms: int = 5000,
        request_timeout_seconds: float = 20.0,
    ) -> None:
        self._api_key = api_key.strip()
        self._api_secret = api_secret.encode("utf-8")
        self._base_url = base_url.rstrip("/")
        self._history_hours = history_hours
        self._cache_seconds = cache_seconds
        self._recv_window_ms = recv_window_ms
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(request_timeout_seconds),
            follow_redirects=False,
        )
        self._lock = asyncio.Lock()
        self._server_offset_ms = 0
        self._server_offset_synced_at = 0.0
        self._cache_at = 0.0
        self._cache_start_ms = 0
        self._cache_end_ms = 0
        self._cache: list[dict[str, Any]] = []

    async def close(self) -> None:
        await self._http.aclose()

    async def _sync_server_time(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._server_offset_synced_at < 300:
            return
        try:
            response = await self._http.get(f"{self._base_url}/api/v3/time")
            response.raise_for_status()
            payload = response.json()
            server_time = int(payload["serverTime"])
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise BinanceAPIError("No se pudo sincronizar la hora con Binance") from exc
        local_time = int(time.time() * 1000)
        self._server_offset_ms = server_time - local_time
        self._server_offset_synced_at = now

    def _now_ms(self) -> int:
        return int(time.time() * 1000) + self._server_offset_ms

    def _signed_url(self, path: str, params: dict[str, Any]) -> str:
        query = urlencode(params)
        signature = hmac.new(self._api_secret, query.encode("utf-8"), hashlib.sha256).hexdigest()
        return f"{self._base_url}{path}?{query}&signature={signature}"

    async def _request_history(
        self,
        *,
        start_time_ms: int,
        end_time_ms: int,
        retry_time_sync: bool = True,
    ) -> list[dict[str, Any]]:
        await self._sync_server_time()
        now_ms = self._now_ms()
        end_time_ms = min(end_time_ms, now_ms)
        if end_time_ms <= start_time_ms:
            start_time_ms = max(0, end_time_ms - 60_000)

        params = {
            "startTime": int(start_time_ms),
            "endTime": int(end_time_ms),
            "limit": 100,
            "recvWindow": self._recv_window_ms,
            "timestamp": now_ms,
        }
        try:
            response = await self._http.get(
                self._signed_url("/sapi/v1/pay/transactions", params),
                headers={"X-MBX-APIKEY": self._api_key},
            )
        except httpx.HTTPError as exc:
            raise BinanceAPIError("No se pudo conectar con Binance") from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise BinanceAPIError(
                f"Binance devolvió una respuesta no JSON (HTTP {response.status_code})",
                http_status=response.status_code,
            ) from exc

        if response.status_code >= 400:
            code = payload.get("code") if isinstance(payload, dict) else None
            if code == -1021 and retry_time_sync:
                await self._sync_server_time(force=True)
                return await self._request_history(
                    start_time_ms=start_time_ms,
                    end_time_ms=end_time_ms,
                    retry_time_sync=False,
                )
            message = payload.get("msg") if isinstance(payload, dict) else str(payload)
            raise BinanceAPIError(
                f"Binance HTTP {response.status_code}: {message}",
                code=code,
                http_status=response.status_code,
            )

        if not isinstance(payload, dict) or not payload.get("success"):
            code = payload.get("code") if isinstance(payload, dict) else None
            message = payload.get("message") if isinstance(payload, dict) else None
            raise BinanceAPIError(
                f"Binance Pay: {code or 'sin código'} {message or 'respuesta inesperada'}",
                code=code,
                http_status=response.status_code,
            )
        data = payload.get("data")
        if not isinstance(data, list):
            raise BinanceAPIError("La respuesta de Binance no contiene una lista de transacciones")
        return [item for item in data if isinstance(item, dict)]

    async def get_history(
        self,
        *,
        start_time_ms: int,
        end_time_ms: int,
        force_refresh: bool = False,
    ) -> list[dict[str, Any]]:
        async with self._lock:
            now = time.monotonic()
            cache_covers_window = (
                self._cache_at > 0
                and self._cache_start_ms <= start_time_ms
                and self._cache_end_ms >= end_time_ms
            )
            if (
                not force_refresh
                and cache_covers_window
                and now - self._cache_at <= self._cache_seconds
            ):
                return list(self._cache)

            history = await self._request_history(
                start_time_ms=start_time_ms,
                end_time_ms=end_time_ms,
            )
            self._cache = history
            self._cache_at = now
            self._cache_start_ms = start_time_ms
            self._cache_end_ms = end_time_ms
            return list(history)

    async def get_recent_history(self, *, force_refresh: bool = False) -> list[dict[str, Any]]:
        await self._sync_server_time()
        end_ms = self._now_ms()
        start_ms = end_ms - self._history_hours * 60 * 60 * 1000
        return await self.get_history(
            start_time_ms=start_ms,
            end_time_ms=end_ms,
            force_refresh=force_refresh,
        )

    @staticmethod
    def _decimal_field(transaction: dict[str, Any], field: str) -> Decimal | None:
        try:
            return Decimal(str(transaction.get(field)))
        except (InvalidOperation, TypeError, ValueError):
            return None

    @classmethod
    def extract_received_amount(
        cls,
        transaction: dict[str, Any],
        currency: str = "USDT",
    ) -> Decimal | None:
        """Return a positive received amount in the requested settlement currency."""

        actual_currency = str(transaction.get("currency", "")).upper().strip()
        if actual_currency != currency.upper():
            return None
        amount = cls._decimal_field(transaction, "amount")
        if amount is None or amount <= 0:
            return None
        return amount

    # Kept for compatibility with the previous public helper and existing users.
    @classmethod
    def extract_amount(
        cls,
        transaction: dict[str, Any],
        currency: str = "USDT",
    ) -> Decimal | None:
        return cls.extract_received_amount(transaction, currency)

    @staticmethod
    def _transaction_time_ms(transaction: dict[str, Any]) -> int | None:
        try:
            return int(transaction.get("transactionTime"))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _receiver_identifiers(transaction: dict[str, Any]) -> frozenset[str]:
        receiver = transaction.get("receiverInfo")
        if not isinstance(receiver, dict):
            return frozenset()
        values: set[str] = set()
        for field in ("accountId", "binanceId", "email", "phoneNumber"):
            value = str(receiver.get(field, "")).strip().lower()
            if value:
                values.add(value)
        return frozenset(values)

    @staticmethod
    def _receiver_account_id(transaction: dict[str, Any]) -> str:
        receiver = transaction.get("receiverInfo")
        if not isinstance(receiver, dict):
            return ""
        return str(receiver.get("accountId", "")).strip()

    @staticmethod
    def _observed_order_ids(
        history: Iterable[dict[str, Any]], *, limit: int = 5
    ) -> tuple[str, ...]:
        observed: list[tuple[int, str]] = []
        for transaction in history:
            transaction_time = BinancePayHistoryClient._transaction_time_ms(transaction) or 0
            raw_id = str(transaction.get("transactionId", "")).strip()
            if not raw_id:
                continue
            observed.append((transaction_time, display_transaction_reference(raw_id)))
        observed.sort(key=lambda item: item[0], reverse=True)
        unique: list[str] = []
        for _time_ms, value in observed:
            if value and value not in unique:
                unique.append(value)
            if len(unique) >= limit:
                break
        return tuple(unique)

    @staticmethod
    def _utc_datetime(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    async def _history_for_deposit(
        self,
        *,
        not_before: datetime,
        force_refresh: bool,
    ) -> tuple[list[dict[str, Any]], int, int]:
        await self._sync_server_time()
        end_ms = self._now_ms()
        not_before_utc = self._utc_datetime(not_before)
        start_dt = not_before_utc - _PAYMENT_CLOCK_TOLERANCE
        minimum_start = datetime.now(UTC) - _MAX_BINANCE_QUERY_SPAN
        if start_dt < minimum_start:
            start_dt = minimum_start
        start_ms = int(start_dt.timestamp() * 1000)
        history = await self.get_history(
            start_time_ms=start_ms,
            end_time_ms=end_ms,
            force_refresh=force_refresh,
        )
        return history, start_ms, end_ms

    async def verify_received_transaction(
        self,
        *,
        transaction_id: str,
        expected_pay_id: str,
        expected_amount: Decimal,
        not_before: datetime,
        force_refresh: bool = False,
    ) -> VerifiedPayTransaction:
        claimed_key = transaction_reference_key(transaction_id)
        history, start_ms, end_ms = await self._history_for_deposit(
            not_before=not_before,
            force_refresh=force_refresh,
        )

        match: dict[str, Any] | None = None
        for item in history:
            if claimed_key in transaction_reference_candidates(item):
                match = item
                break

        if match is None:
            raise BinanceTransactionNotFound(
                inspected_count=len(history),
                observed_transaction_ids=self._observed_order_ids(history),
                start_time_ms=start_ms,
                end_time_ms=end_ms,
            )

        actual_id = str(match.get("transactionId", "")).strip() or transaction_id
        actual_customer_id = display_transaction_reference(actual_id)
        order_type = str(match.get("orderType", "")).upper().strip()
        if order_type and order_type not in _ACCEPTED_INCOME_ORDER_TYPES:
            raise BinanceTransactionMismatch(
                "unsupported_order_type",
                transaction_id=actual_customer_id,
            )

        actual_currency = str(match.get("currency", "")).upper().strip()
        if actual_currency != "USDT":
            raise BinanceTransactionMismatch("currency", transaction_id=actual_customer_id)

        raw_amount = self._decimal_field(match, "amount")
        if raw_amount is None:
            raise BinanceTransactionMismatch("amount_missing", transaction_id=actual_customer_id)
        if raw_amount <= 0:
            raise BinanceTransactionMismatch("outgoing", transaction_id=actual_customer_id)
        if raw_amount != expected_amount:
            raise BinanceTransactionMismatch("amount", transaction_id=actual_customer_id)

        transaction_time_ms = self._transaction_time_ms(match)
        if transaction_time_ms is None:
            raise BinanceTransactionMismatch(
                "transaction_time",
                transaction_id=actual_customer_id,
            )
        not_before_utc = self._utc_datetime(not_before)
        minimum_time_ms = int((not_before_utc - _PAYMENT_CLOCK_TOLERANCE).timestamp() * 1000)
        maximum_time_ms = int((datetime.now(UTC) + _PAYMENT_CLOCK_TOLERANCE).timestamp() * 1000)
        if transaction_time_ms < minimum_time_ms:
            raise BinanceTransactionMismatch("too_old", transaction_id=actual_customer_id)
        if transaction_time_ms > maximum_time_ms:
            raise BinanceTransactionMismatch("future", transaction_id=actual_customer_id)

        # The history endpoint belongs to the account represented by the API key.
        # For a C2C receiver Binance often returns only receiverInfo.binanceId (UID),
        # while the configured Pay ID is receiverInfo.accountId. Do not compare those
        # different namespaces. If accountId is present, however, it must match.
        configured_pay_id = expected_pay_id.strip().lower()
        receiver_identifiers = self._receiver_identifiers(match)
        receiver_account_id = self._receiver_account_id(match).lower()
        if configured_pay_id:
            if configured_pay_id in receiver_identifiers:
                pass
            elif receiver_account_id:
                raise BinanceTransactionMismatch(
                    "receiver_pay_id",
                    transaction_id=actual_customer_id,
                )

        return VerifiedPayTransaction(
            transaction_id=canonical_transaction_reference(actual_id),
            customer_order_id=actual_customer_id,
            amount=raw_amount,
            currency="USDT",
            transaction_time_ms=transaction_time_ms,
            raw=match,
        )

    async def diagnose(self, *, force_refresh: bool = True) -> BinanceHistoryDiagnostic:
        await self._sync_server_time()
        end_ms = self._now_ms()
        start_ms = end_ms - self._history_hours * 60 * 60 * 1000
        history = await self.get_history(
            start_time_ms=start_ms,
            end_time_ms=end_ms,
            force_refresh=force_refresh,
        )
        incoming_usdt = [
            item for item in history if self.extract_received_amount(item, "USDT") is not None
        ]
        times = [
            transaction_time
            for item in history
            if (transaction_time := self._transaction_time_ms(item)) is not None
        ]
        return BinanceHistoryDiagnostic(
            transaction_count=len(history),
            incoming_usdt_count=len(incoming_usdt),
            latest_transaction_time_ms=max(times) if times else None,
            recent_order_ids=self._observed_order_ids(history),
            start_time_ms=start_ms,
            end_time_ms=end_ms,
        )

    @staticmethod
    def serialize_raw(transaction: dict[str, Any]) -> str:
        return json.dumps(transaction, ensure_ascii=False, separators=(",", ":"))
