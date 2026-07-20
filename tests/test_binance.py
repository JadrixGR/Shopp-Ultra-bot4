from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.services.binance import (
    BinancePayHistoryClient,
    BinanceTransactionMismatch,
    BinanceTransactionNotFound,
    canonical_transaction_reference,
    display_transaction_reference,
    extract_transaction_reference,
    transaction_reference_candidates,
    transaction_reference_key,
)


def _incoming_transaction(
    *,
    transaction_id: str = "M_P_442711457387806720",
    amount: str = "0.10",
    currency: str = "USDT",
    order_type: str = "C2C",
    receiver_info: dict[str, str] | None = None,
) -> dict[str, object]:
    return {
        "orderType": order_type,
        "transactionId": transaction_id,
        "transactionTime": int(datetime.now(UTC).timestamp() * 1000),
        "amount": amount,
        "currency": currency,
        "payerInfo": {"name": "Buyer"},
        "receiverInfo": receiver_info or {"binanceId": "99887766"},
    }


async def _install_history(
    monkeypatch: pytest.MonkeyPatch,
    client: BinancePayHistoryClient,
    history: list[dict[str, object]],
) -> None:
    async def fake_history_for_deposit(*, not_before: datetime, force_refresh: bool):
        del not_before, force_refresh
        return history, 1_700_000_000_000, 1_800_000_000_000

    monkeypatch.setattr(client, "_history_for_deposit", fake_history_for_deposit)


@pytest.mark.asyncio
async def test_numeric_order_id_matches_prefixed_binance_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transaction = _incoming_transaction()
    client = BinancePayHistoryClient(api_key="key", api_secret="secret")
    await _install_history(monkeypatch, client, [transaction])

    result = await client.verify_received_transaction(
        transaction_id="442711457387806720",
        expected_pay_id="123456789",
        expected_amount=Decimal("0.10"),
        not_before=datetime.now(UTC) - timedelta(minutes=1),
    )

    assert result.transaction_id == "M_P_442711457387806720"
    assert result.customer_order_id == "442711457387806720"
    assert result.amount == Decimal("0.10")
    await client.close()


@pytest.mark.asyncio
async def test_receiver_uid_is_not_confused_with_pay_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # For the receiver perspective Binance may return only binanceId (UID),
    # while the configured value is the Pay ID (accountId). The signed history
    # already belongs to the receiving account, so this must not be rejected.
    transaction = _incoming_transaction(receiver_info={"binanceId": "99887766"})
    client = BinancePayHistoryClient(api_key="key", api_secret="secret")
    await _install_history(monkeypatch, client, [transaction])

    result = await client.verify_received_transaction(
        transaction_id="442711457387806720",
        expected_pay_id="123456789",
        expected_amount=Decimal("0.10"),
        not_before=datetime.now(UTC) - timedelta(minutes=1),
    )

    assert result.amount == Decimal("0.10")
    await client.close()


@pytest.mark.asyncio
async def test_explicit_receiver_account_id_mismatch_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transaction = _incoming_transaction(
        receiver_info={"binanceId": "99887766", "accountId": "OTHER-PAY-ID"}
    )
    client = BinancePayHistoryClient(api_key="key", api_secret="secret")
    await _install_history(monkeypatch, client, [transaction])

    with pytest.raises(BinanceTransactionMismatch, match="receiver_pay_id"):
        await client.verify_received_transaction(
            transaction_id="442711457387806720",
            expected_pay_id="EXPECTED-PAY-ID",
            expected_amount=Decimal("0.10"),
            not_before=datetime.now(UTC) - timedelta(minutes=1),
        )
    await client.close()


@pytest.mark.asyncio
async def test_outgoing_transaction_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    transaction = _incoming_transaction(amount="-0.10")
    client = BinancePayHistoryClient(api_key="key", api_secret="secret")
    await _install_history(monkeypatch, client, [transaction])

    with pytest.raises(BinanceTransactionMismatch, match="outgoing"):
        await client.verify_received_transaction(
            transaction_id="442711457387806720",
            expected_pay_id="123456789",
            expected_amount=Decimal("0.10"),
            not_before=datetime.now(UTC) - timedelta(minutes=1),
        )
    await client.close()


@pytest.mark.asyncio
async def test_non_usdt_top_level_currency_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transaction = _incoming_transaction(currency="BNB", amount="0.10")
    transaction["fundsDetail"] = [{"currency": "USDT", "amount": "0.10"}]
    client = BinancePayHistoryClient(api_key="key", api_secret="secret")
    await _install_history(monkeypatch, client, [transaction])

    with pytest.raises(BinanceTransactionMismatch, match="currency"):
        await client.verify_received_transaction(
            transaction_id="442711457387806720",
            expected_pay_id="123456789",
            expected_amount=Decimal("0.10"),
            not_before=datetime.now(UTC) - timedelta(minutes=1),
        )
    await client.close()


@pytest.mark.asyncio
async def test_alternate_nested_order_id_field_is_matched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transaction = _incoming_transaction(transaction_id="")
    transaction["extend"] = {"orderId": "M-P-442711457387806720"}
    client = BinancePayHistoryClient(api_key="key", api_secret="secret")
    await _install_history(monkeypatch, client, [transaction])

    result = await client.verify_received_transaction(
        transaction_id="Order ID: 442711457387806720",
        expected_pay_id="123456789",
        expected_amount=Decimal("0.10"),
        not_before=datetime.now(UTC) - timedelta(minutes=1),
    )

    assert result.customer_order_id == "442711457387806720"
    await client.close()


@pytest.mark.asyncio
async def test_not_found_contains_api_diagnostic(monkeypatch: pytest.MonkeyPatch) -> None:
    transaction = _incoming_transaction(transaction_id="M_P_111111111111111111")
    client = BinancePayHistoryClient(api_key="key", api_secret="secret")
    await _install_history(monkeypatch, client, [transaction])

    with pytest.raises(BinanceTransactionNotFound) as exc_info:
        await client.verify_received_transaction(
            transaction_id="442711457387806720",
            expected_pay_id="123456789",
            expected_amount=Decimal("0.10"),
            not_before=datetime.now(UTC) - timedelta(minutes=1),
        )

    assert exc_info.value.inspected_count == 1
    assert exc_info.value.observed_transaction_ids == ("111111111111111111",)
    await client.close()


def test_reference_normalization_accepts_number_without_prefix() -> None:
    numeric = "442711457387806720"
    assert extract_transaction_reference(numeric) == numeric
    assert extract_transaction_reference(f"M_P_{numeric}") == numeric
    assert extract_transaction_reference(f"Order ID: {numeric}") == numeric
    assert transaction_reference_key(f"M-P-{numeric}") == numeric
    assert canonical_transaction_reference(numeric) == f"M_P_{numeric}"
    assert display_transaction_reference(f"M_P_{numeric}") == numeric


def test_reference_candidates_ignore_account_ids() -> None:
    transaction = {
        "transactionId": "M_P_442711457387806720",
        "receiverInfo": {"accountId": "111111111111111111", "binanceId": "222222222"},
    }
    assert transaction_reference_candidates(transaction) == frozenset({"442711457387806720"})


def test_extract_received_amount_requires_positive_top_level_usdt() -> None:
    assert BinancePayHistoryClient.extract_received_amount(
        {"currency": "USDT", "amount": "10.00"}
    ) == Decimal("10.00")
    assert (
        BinancePayHistoryClient.extract_received_amount({"currency": "USDT", "amount": "-10.00"})
        is None
    )
    assert (
        BinancePayHistoryClient.extract_received_amount(
            {
                "currency": "BNB",
                "amount": "0.01",
                "fundsDetail": [{"currency": "USDT", "amount": "10.00"}],
            }
        )
        is None
    )
