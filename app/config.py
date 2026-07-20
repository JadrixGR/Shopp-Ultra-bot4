from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-backed application configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    bot_token: SecretStr = Field(alias="BOT_TOKEN")
    bot_id: int | None = Field(default=None, alias="BOT_ID")
    admin_ids_raw: str = Field(alias="ADMIN_IDS")

    database_url: str = Field(default="sqlite+aiosqlite:///./data/shop.db", alias="DATABASE_URL")
    store_name: str = Field(default="Shop Ultra", alias="STORE_NAME")
    binance_pay_id: str = Field(default="", alias="BINANCE_PAY_ID")
    binance_pay_name: str = Field(default="", alias="BINANCE_PAY_NAME")
    support_username: str = Field(default="", alias="SUPPORT_USERNAME")
    bonus_tiers: str = Field(default="50:2,100:5", alias="BONUS_TIERS")
    min_deposit: Decimal = Field(default=Decimal("1.00"), alias="MIN_DEPOSIT")

    binance_api_key: SecretStr | None = Field(default=None, alias="BINANCE_API_KEY")
    binance_api_secret: SecretStr | None = Field(default=None, alias="BINANCE_API_SECRET")
    binance_base_url: str = Field(default="https://api.binance.com", alias="BINANCE_BASE_URL")
    binance_history_hours: int = Field(default=72, alias="BINANCE_HISTORY_HOURS")
    binance_cache_seconds: int = Field(default=15, alias="BINANCE_CACHE_SECONDS")
    binance_recv_window_ms: int = Field(default=5000, alias="BINANCE_RECV_WINDOW_MS")
    binance_request_timeout_seconds: float = Field(
        default=20.0, alias="BINANCE_REQUEST_TIMEOUT_SECONDS"
    )
    binance_verify_attempts: int = Field(default=2, alias="BINANCE_VERIFY_ATTEMPTS")
    binance_verify_retry_delay_seconds: float = Field(
        default=8.0, alias="BINANCE_VERIFY_RETRY_DELAY_SECONDS"
    )

    # Multiple external API providers. Credentials are stored in this JSON file.
    api_providers_file: str = Field(default="data/providers.json", alias="API_PROVIDERS_FILE")

    # ProdSeller external catalog and instant fulfillment.
    prodseller_enabled: bool = Field(default=False, alias="PRODSELLER_ENABLED")
    prodseller_base_url: str = Field(default="http://51.77.244.194/v1", alias="PRODSELLER_BASE_URL")
    prodseller_api_key: SecretStr | None = Field(default=None, alias="PRODSELLER_API_KEY")
    prodseller_allow_insecure_http: bool = Field(
        default=False, alias="PRODSELLER_ALLOW_INSECURE_HTTP"
    )
    prodseller_markup_percent: Decimal = Field(
        default=Decimal("20"), alias="PRODSELLER_MARKUP_PERCENT"
    )
    prodseller_sync_prices: bool = Field(default=False, alias="PRODSELLER_SYNC_PRICES")
    prodseller_allow_below_cost: bool = Field(default=False, alias="PRODSELLER_ALLOW_BELOW_COST")
    prodseller_auto_sync_minutes: int = Field(default=10, alias="PRODSELLER_AUTO_SYNC_MINUTES")
    prodseller_cache_seconds: int = Field(default=60, alias="PRODSELLER_CACHE_SECONDS")
    prodseller_timeout_seconds: float = Field(default=20.0, alias="PRODSELLER_TIMEOUT_SECONDS")
    prodseller_order_poll_attempts: int = Field(default=4, alias="PRODSELLER_ORDER_POLL_ATTEMPTS")
    prodseller_order_poll_delay_seconds: float = Field(
        default=2.0, alias="PRODSELLER_ORDER_POLL_DELAY_SECONDS"
    )

    verification_cooldown_seconds: int = Field(default=15, alias="VERIFICATION_COOLDOWN_SECONDS")
    drop_pending_updates: bool = Field(default=False, alias="DROP_PENDING_UPDATES")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    @field_validator("bot_id", mode="before")
    @classmethod
    def blank_bot_id_to_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("bot_id")
    @classmethod
    def validate_bot_id(cls, value: int | None) -> int | None:
        if value is not None and value <= 0:
            raise ValueError("BOT_ID must be a positive Telegram bot ID")
        return value

    @field_validator(
        "binance_api_key",
        "binance_api_secret",
        "prodseller_api_key",
        mode="before",
    )
    @classmethod
    def blank_secret_to_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("admin_ids_raw")
    @classmethod
    def validate_admin_ids(cls, value: str) -> str:
        parts = [part.strip() for part in value.split(",") if part.strip()]
        if not parts:
            raise ValueError("ADMIN_IDS must contain at least one Telegram numeric ID")
        for part in parts:
            int(part)
        return ",".join(parts)

    @field_validator("min_deposit")
    @classmethod
    def validate_min_deposit(cls, value: Decimal) -> Decimal:
        if value <= 0:
            raise ValueError("MIN_DEPOSIT must be greater than zero")
        return value.quantize(Decimal("0.01"))

    @field_validator("binance_history_hours")
    @classmethod
    def validate_history_hours(cls, value: int) -> int:
        if value < 1 or value > 24 * 90:
            raise ValueError("BINANCE_HISTORY_HOURS must be between 1 and 2160")
        return value

    @field_validator("binance_verify_attempts")
    @classmethod
    def validate_verify_attempts(cls, value: int) -> int:
        if value < 1 or value > 5:
            raise ValueError("BINANCE_VERIFY_ATTEMPTS must be between 1 and 5")
        return value

    @field_validator(
        "binance_request_timeout_seconds",
        "binance_verify_retry_delay_seconds",
        "prodseller_timeout_seconds",
        "prodseller_order_poll_delay_seconds",
    )
    @classmethod
    def validate_nonnegative_seconds(cls, value: float) -> float:
        if value < 0 or value > 120:
            raise ValueError("Timeout/retry seconds must be between 0 and 120")
        return value

    @field_validator("prodseller_markup_percent")
    @classmethod
    def validate_markup(cls, value: Decimal) -> Decimal:
        if value < 0 or value > 1000:
            raise ValueError("PRODSELLER_MARKUP_PERCENT must be between 0 and 1000")
        return value.quantize(Decimal("0.01"))

    @field_validator("prodseller_auto_sync_minutes")
    @classmethod
    def validate_auto_sync(cls, value: int) -> int:
        if value < 0 or value > 1440:
            raise ValueError("PRODSELLER_AUTO_SYNC_MINUTES must be between 0 and 1440")
        return value

    @field_validator("prodseller_cache_seconds")
    @classmethod
    def validate_provider_cache(cls, value: int) -> int:
        if value < 0 or value > 900:
            raise ValueError("PRODSELLER_CACHE_SECONDS must be between 0 and 900")
        return value

    @field_validator("prodseller_order_poll_attempts")
    @classmethod
    def validate_provider_poll_attempts(cls, value: int) -> int:
        if value < 1 or value > 10:
            raise ValueError("PRODSELLER_ORDER_POLL_ATTEMPTS must be between 1 and 10")
        return value

    @field_validator("prodseller_base_url")
    @classmethod
    def validate_provider_url(cls, value: str) -> str:
        normalized = value.strip().rstrip("/")
        parsed = urlparse(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("PRODSELLER_BASE_URL must be a valid http:// or https:// URL")
        return normalized

    @model_validator(mode="after")
    def validate_prodseller_configuration(self) -> Settings:
        if self.prodseller_enabled and self.prodseller_api_key is None:
            raise ValueError("PRODSELLER_ENABLED=true requires PRODSELLER_API_KEY")
        if (
            self.prodseller_enabled
            and self.prodseller_base_url.lower().startswith("http://")
            and not self.prodseller_allow_insecure_http
        ):
            raise ValueError(
                "ProdSeller uses plain HTTP. Set PRODSELLER_ALLOW_INSECURE_HTTP=true "
                "only if you accept that the API key and delivered keys travel without TLS."
            )
        return self

    @property
    def admin_ids(self) -> frozenset[int]:
        return frozenset(int(part) for part in self.admin_ids_raw.split(","))

    @property
    def primary_admin_id(self) -> int:
        return next(iter(self.admin_ids))

    @property
    def binance_verification_enabled(self) -> bool:
        return bool(self.binance_api_key and self.binance_api_secret)

    @property
    def prodseller_configured(self) -> bool:
        return bool(self.prodseller_enabled and self.prodseller_api_key)

    def ensure_local_directories(self) -> None:
        if self.database_url.startswith("sqlite"):
            marker = "///./"
            if marker in self.database_url:
                relative = self.database_url.split(marker, 1)[1]
                Path(relative).parent.mkdir(parents=True, exist_ok=True)
