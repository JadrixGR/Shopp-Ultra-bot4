from __future__ import annotations

import html
from decimal import Decimal, InvalidOperation


def h(value: object) -> str:
    return html.escape(str(value), quote=False)


def h_truncate(value: object, max_escaped_length: int) -> str:
    """HTML-escape while keeping the escaped result below a safe limit."""

    source = str(value)
    chunks: list[str] = []
    used = 0
    truncated = False
    for character in source:
        escaped = html.escape(character, quote=False)
        if used + len(escaped) > max_escaped_length:
            truncated = True
            break
        chunks.append(escaped)
        used += len(escaped)
    if truncated and used + 1 <= max_escaped_length:
        chunks.append("…")
    return "".join(chunks)


def money(value: Decimal | int | float | str) -> str:
    return f"{Decimal(str(value)):.2f}"


def parse_money(text: str) -> Decimal:
    normalized = text.strip().replace(",", ".").replace("$", "").replace("USDT", "")
    try:
        value = Decimal(normalized)
    except InvalidOperation as exc:
        raise ValueError("invalid amount") from exc
    if not value.is_finite():
        raise ValueError("invalid amount")
    return value.quantize(Decimal("0.01"))


def shorten(value: str, limit: int) -> str:
    value = value.strip()
    if len(value) <= limit:
        return value
    return value[: max(1, limit - 1)] + "…"
