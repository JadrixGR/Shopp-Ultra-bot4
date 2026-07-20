from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import AppSetting

STORE_NAME = "store_name"
BINANCE_PAY_ID = "binance_pay_id"
BINANCE_PAY_NAME = "binance_pay_name"
SUPPORT_USERNAME = "support_username"
BONUS_TIERS = "bonus_tiers"

EDITABLE_SETTINGS = {
    STORE_NAME,
    BINANCE_PAY_ID,
    BINANCE_PAY_NAME,
    SUPPORT_USERNAME,
    BONUS_TIERS,
}


@dataclass(frozen=True, slots=True)
class StoreProfile:
    name: str
    binance_pay_id: str
    binance_pay_name: str
    support_username: str
    bonus_tiers_raw: str


async def seed_runtime_settings(session: AsyncSession, config: Settings) -> None:
    defaults = {
        STORE_NAME: config.store_name,
        BINANCE_PAY_ID: config.binance_pay_id,
        BINANCE_PAY_NAME: config.binance_pay_name,
        SUPPORT_USERNAME: config.support_username,
        BONUS_TIERS: config.bonus_tiers,
    }
    existing = set((await session.scalars(select(AppSetting.key))).all())
    for key, value in defaults.items():
        if key not in existing:
            session.add(AppSetting(key=key, value=value))
    await session.commit()


async def get_store_profile(session: AsyncSession) -> StoreProfile:
    rows = (await session.scalars(select(AppSetting))).all()
    values = {row.key: row.value for row in rows}
    return StoreProfile(
        name=values.get(STORE_NAME, "Shop Ultra").strip() or "Shop Ultra",
        binance_pay_id=values.get(BINANCE_PAY_ID, "").strip(),
        binance_pay_name=values.get(BINANCE_PAY_NAME, "").strip(),
        support_username=values.get(SUPPORT_USERNAME, "").strip().lstrip("@"),
        bonus_tiers_raw=values.get(BONUS_TIERS, "50:2,100:5").strip(),
    )


async def set_runtime_setting(session: AsyncSession, key: str, value: str) -> None:
    if key not in EDITABLE_SETTINGS:
        raise ValueError(f"Setting {key!r} is not editable")
    row = await session.get(AppSetting, key)
    if row is None:
        session.add(AppSetting(key=key, value=value))
    else:
        row.value = value
    await session.commit()


def parse_bonus_tiers(raw: str) -> list[tuple[Decimal, Decimal]]:
    """Parse ``threshold:percent`` entries and return ascending thresholds."""

    if not raw.strip():
        return []
    tiers: list[tuple[Decimal, Decimal]] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            threshold_raw, percent_raw = entry.split(":", 1)
            threshold = Decimal(threshold_raw.strip()).quantize(Decimal("0.01"))
            percent = Decimal(percent_raw.strip()).quantize(Decimal("0.01"))
        except (ValueError, InvalidOperation) as exc:
            raise ValueError("Formato inválido. Usa, por ejemplo: 50:2,100:5") from exc
        if threshold <= 0 or percent < 0 or percent > 100:
            raise ValueError("Los límites deben ser positivos y el bono entre 0 y 100")
        tiers.append((threshold, percent))
    tiers.sort(key=lambda item: item[0])
    return tiers


def calculate_bonus(amount: Decimal, raw_tiers: str) -> tuple[Decimal, Decimal]:
    selected_percent = Decimal("0.00")
    for threshold, percent in parse_bonus_tiers(raw_tiers):
        if amount >= threshold:
            selected_percent = percent
        else:
            break
    bonus = (amount * selected_percent / Decimal("100")).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    return selected_percent, bonus


def format_bonus_tiers(raw: str, language: str = "es") -> str:
    tiers = parse_bonus_tiers(raw)
    if not tiers:
        return "Sin bonos" if language == "es" else "No bonuses"
    arrow = "recibe" if language == "es" else "receive"
    return "\n".join(
        f"• ${threshold:.2f}+ → +{percent:g}% ({arrow} ${(threshold * (Decimal('1') + percent / Decimal('100'))):.2f})"
        for threshold, percent in tiers
    )


def provider_auto_publish_key(provider_code: str) -> str:
    normalized = provider_code.strip().lower()[:32]
    return f"api_auto_publish:{normalized}"


async def get_provider_auto_publish(
    session: AsyncSession,
    provider_code: str,
    *,
    default: bool = False,
) -> bool:
    row = await session.get(AppSetting, provider_auto_publish_key(provider_code))
    if row is None:
        return default
    return row.value.strip().lower() in {"1", "true", "yes", "si", "sí", "on"}


async def set_provider_auto_publish(
    session: AsyncSession,
    provider_code: str,
    enabled: bool,
) -> None:
    key = provider_auto_publish_key(provider_code)
    row = await session.get(AppSetting, key)
    value = "true" if enabled else "false"
    if row is None:
        session.add(AppSetting(key=key, value=value))
    else:
        row.value = value
    await session.commit()
