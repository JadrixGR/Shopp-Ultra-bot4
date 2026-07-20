from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from app.config import Settings
from app.services.canboso_buyer import CanbosoBuyerClient
from app.services.prodseller import ProdSellerClient

logger = logging.getLogger(__name__)

PRODSELLER_ADAPTER_CODE = "prodseller_v1"
CANBOSO_ADAPTER_CODE = "canboso_buyer_v1"
SUPPORTED_ADAPTERS = {PRODSELLER_ADAPTER_CODE, CANBOSO_ADAPTER_CODE}
ADAPTER_LABELS = {
    PRODSELLER_ADAPTER_CODE: "ProdSeller API v1",
    CANBOSO_ADAPTER_CODE: "Canboso Buyer API 1.2",
}
_CODE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{1,30}[a-z0-9]$")


class ProviderConfigError(ValueError):
    pass


def provider_slug(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    if not normalized:
        normalized = "provider"
    normalized = normalized[:32].strip("_")
    if len(normalized) < 3:
        normalized = f"api_{normalized}"[:32]
    return normalized


def _decimal(value: Any, *, default: str) -> Decimal:
    if value is None or value == "":
        value = default
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ProviderConfigError(f"Valor decimal inválido: {value!r}") from exc
    return result.quantize(Decimal("0.01"))


def _integer(value: Any, *, default: int, minimum: int, maximum: int, label: str) -> int:
    if value is None or value == "":
        return default
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ProviderConfigError(f"{label} debe ser un número entero") from exc
    if result < minimum or result > maximum:
        raise ProviderConfigError(f"{label} debe estar entre {minimum} y {maximum}")
    return result


def _float(value: Any, *, default: float, minimum: float, maximum: float, label: str) -> float:
    if value is None or value == "":
        return default
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ProviderConfigError(f"{label} debe ser numérico") from exc
    if result < minimum or result > maximum:
        raise ProviderConfigError(f"{label} debe estar entre {minimum} y {maximum}")
    return result


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    code: str
    name: str
    base_url: str
    api_key: str
    api_key_header: str = "X-API-Key"
    adapter: str = PRODSELLER_ADAPTER_CODE
    enabled: bool = True
    allow_insecure_http: bool = False
    markup_percent: Decimal = Decimal("20.00")
    auto_sync_minutes: int = 10
    cache_seconds: int = 60
    timeout_seconds: float = 20.0
    allow_below_cost: bool = False
    order_poll_attempts: int = 4
    order_poll_delay_seconds: float = 2.0

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ProviderConfig:
        name = str(raw.get("name") or "").strip()
        code = str(raw.get("code") or provider_slug(name)).strip().lower()
        base_url = str(raw.get("base_url") or "").strip().rstrip("/")
        api_key = str(raw.get("api_key") or "").strip()
        api_key_header = str(raw.get("api_key_header") or "X-API-Key").strip()
        adapter = str(raw.get("adapter") or PRODSELLER_ADAPTER_CODE).strip().lower()
        enabled = bool(raw.get("enabled", True))
        allow_http = bool(raw.get("allow_insecure_http", False))

        if not name:
            raise ProviderConfigError("El proveedor debe tener un nombre")
        if not _CODE_PATTERN.fullmatch(code):
            raise ProviderConfigError(
                "El código debe tener 3-32 caracteres: minúsculas, números, _ o -"
            )
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ProviderConfigError(f"URL inválida para {name}")
        if parsed.scheme == "http" and enabled and not allow_http:
            raise ProviderConfigError(
                f"{name} usa HTTP. Debes aceptar explícitamente el transporte sin cifrado."
            )
        if enabled and not api_key:
            raise ProviderConfigError(f"{name} está activo pero no tiene API Key")
        if not api_key_header or any(ch.isspace() for ch in api_key_header):
            raise ProviderConfigError("El nombre del header de autenticación es inválido")
        if adapter not in SUPPORTED_ADAPTERS:
            raise ProviderConfigError(
                f"Adaptador no soportado: {adapter}. Disponibles: {', '.join(SUPPORTED_ADAPTERS)}"
            )

        markup = _decimal(raw.get("markup_percent"), default="20")
        if markup < 0 or markup > 1000:
            raise ProviderConfigError("El margen debe estar entre 0 y 1000%")

        return cls(
            code=code,
            name=name[:80],
            base_url=base_url,
            api_key=api_key,
            api_key_header=api_key_header[:80],
            adapter=adapter,
            enabled=enabled,
            allow_insecure_http=allow_http,
            markup_percent=markup,
            auto_sync_minutes=_integer(
                raw.get("auto_sync_minutes"),
                default=10,
                minimum=0,
                maximum=1440,
                label="Sincronización automática",
            ),
            cache_seconds=_integer(
                raw.get("cache_seconds"),
                default=60,
                minimum=0,
                maximum=900,
                label="Caché",
            ),
            timeout_seconds=_float(
                raw.get("timeout_seconds"),
                default=20.0,
                minimum=1.0,
                maximum=120.0,
                label="Timeout",
            ),
            allow_below_cost=bool(raw.get("allow_below_cost", False)),
            order_poll_attempts=_integer(
                raw.get("order_poll_attempts"),
                default=4,
                minimum=1,
                maximum=10,
                label="Intentos de entrega",
            ),
            order_poll_delay_seconds=_float(
                raw.get("order_poll_delay_seconds"),
                default=2.0,
                minimum=0.0,
                maximum=120.0,
                label="Espera de entrega",
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "name": self.name,
            "adapter": self.adapter,
            "enabled": self.enabled,
            "base_url": self.base_url,
            "api_key": self.api_key,
            "api_key_header": self.api_key_header,
            "allow_insecure_http": self.allow_insecure_http,
            "markup_percent": str(self.markup_percent),
            "auto_sync_minutes": self.auto_sync_minutes,
            "cache_seconds": self.cache_seconds,
            "timeout_seconds": self.timeout_seconds,
            "allow_below_cost": self.allow_below_cost,
            "order_poll_attempts": self.order_poll_attempts,
            "order_poll_delay_seconds": self.order_poll_delay_seconds,
        }


@dataclass(slots=True)
class ProviderRuntime:
    config: ProviderConfig
    client: ProdSellerClient


class ProviderRegistry:
    def __init__(self, runtimes: Iterable[ProviderRuntime] = ()) -> None:
        self._runtimes = {runtime.config.code: runtime for runtime in runtimes}

    def get(self, code: str | None) -> ProviderRuntime | None:
        if not code:
            return None
        return self._runtimes.get(code)

    def values(self) -> tuple[ProviderRuntime, ...]:
        return tuple(sorted(self._runtimes.values(), key=lambda item: item.config.name.lower()))

    def items(self) -> tuple[tuple[str, ProviderRuntime], ...]:
        """Return provider entries using a stable, mapping-compatible interface.

        Older handlers treated ``ProviderRegistry`` as a mapping and called
        ``items()``. Keeping this helper prevents runtime failures when code from
        different releases is used with the current registry implementation.
        """

        return tuple((runtime.config.code, runtime) for runtime in self.values())

    def keys(self) -> tuple[str, ...]:
        return tuple(code for code, _runtime in self.items())

    def __contains__(self, code: object) -> bool:
        return isinstance(code, str) and code in self._runtimes

    def __len__(self) -> int:
        return len(self._runtimes)

    async def close(self) -> None:
        for runtime in self._runtimes.values():
            await runtime.client.close()


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "providers": []}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProviderConfigError(f"No se pudo leer {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ProviderConfigError("El archivo de proveedores debe contener un objeto JSON")
    providers = raw.get("providers", [])
    if not isinstance(providers, list):
        raise ProviderConfigError("providers debe ser una lista")
    return raw


def save_provider_configs(path: Path, configs: Iterable[ProviderConfig]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "providers": [config.to_dict() for config in configs],
    }
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def _legacy_prodseller(settings: Settings) -> ProviderConfig | None:
    if not settings.prodseller_configured or settings.prodseller_api_key is None:
        return None
    return ProviderConfig.from_dict(
        {
            "code": "prodseller",
            "name": "ProdSeller",
            "adapter": PRODSELLER_ADAPTER_CODE,
            "enabled": True,
            "base_url": settings.prodseller_base_url,
            "api_key": settings.prodseller_api_key.get_secret_value(),
            "api_key_header": "X-API-Key",
            "allow_insecure_http": settings.prodseller_allow_insecure_http,
            "markup_percent": str(settings.prodseller_markup_percent),
            "auto_sync_minutes": settings.prodseller_auto_sync_minutes,
            "cache_seconds": settings.prodseller_cache_seconds,
            "timeout_seconds": settings.prodseller_timeout_seconds,
            "allow_below_cost": settings.prodseller_allow_below_cost,
            "order_poll_attempts": settings.prodseller_order_poll_attempts,
            "order_poll_delay_seconds": settings.prodseller_order_poll_delay_seconds,
        }
    )


def load_provider_configs(settings: Settings) -> list[ProviderConfig]:
    path = Path(settings.api_providers_file)
    raw = _read_json(path)
    configs: list[ProviderConfig] = []
    seen: set[str] = set()
    for item in raw.get("providers", []):
        if not isinstance(item, dict):
            raise ProviderConfigError("Cada proveedor debe ser un objeto JSON")
        config = ProviderConfig.from_dict(item)
        if config.code in seen:
            raise ProviderConfigError(f"Código de proveedor duplicado: {config.code}")
        configs.append(config)
        seen.add(config.code)

    legacy = _legacy_prodseller(settings)
    if legacy is not None and legacy.code not in seen:
        configs.append(legacy)
        save_provider_configs(path, configs)
        logger.info("Imported legacy PRODSELLER_* settings into %s", path)
    return configs


def build_provider_registry(settings: Settings) -> ProviderRegistry:
    runtimes: list[ProviderRuntime] = []
    for config in load_provider_configs(settings):
        if not config.enabled:
            continue
        client_class: type[ProdSellerClient]
        if config.adapter == CANBOSO_ADAPTER_CODE:
            client_class = CanbosoBuyerClient
        else:
            client_class = ProdSellerClient
        client = client_class(
            api_key=config.api_key,
            base_url=config.base_url,
            allow_insecure_http=config.allow_insecure_http,
            timeout_seconds=config.timeout_seconds,
            cache_seconds=config.cache_seconds,
            api_key_header=config.api_key_header,
            provider_name=config.name,
        )
        runtimes.append(ProviderRuntime(config=config, client=client))
    return ProviderRegistry(runtimes)
