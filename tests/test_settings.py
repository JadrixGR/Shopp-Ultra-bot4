from decimal import Decimal

import pytest

from app.services.settings import calculate_bonus, parse_bonus_tiers


def test_bonus_uses_highest_matching_tier() -> None:
    percent, bonus = calculate_bonus(Decimal("125.00"), "50:2,100:5")
    assert percent == Decimal("5.00")
    assert bonus == Decimal("6.25")


def test_bonus_below_first_tier_is_zero() -> None:
    percent, bonus = calculate_bonus(Decimal("49.99"), "50:2,100:5")
    assert percent == Decimal("0.00")
    assert bonus == Decimal("0.00")


def test_invalid_bonus_tier_is_rejected() -> None:
    with pytest.raises(ValueError):
        parse_bonus_tiers("50=2")
