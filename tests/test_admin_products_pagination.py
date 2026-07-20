from __future__ import annotations

from decimal import Decimal

from app.handlers.admin import _ADMIN_PRODUCTS_PAGE_SIZE, _build_admin_products_page
from app.models import Product
from app.services.catalog import ProductWithStock


def _product_item(index: int, *, external: bool) -> ProductWithStock:
    product = Product(
        id=index,
        name=f"Producto {index}",
        description="",
        price=Decimal("1.00"),
        button_emoji="📦",
        active=index % 3 != 0,
        provider_code="provider_one" if external else None,
        external_product_id=f"external-{index}" if external else None,
        provider_in_stock=True if external else None,
    )
    return ProductWithStock(
        product=product,
        stock=1 if external else index,
        external_stock_known=external,
    )


def test_admin_product_list_is_paginated_below_reply_markup_limit() -> None:
    products = [_product_item(index, external=index > 15) for index in range(1, 121)]

    text, markup, page, page_count = _build_admin_products_page(
        products,
        page=0,
        provider_names={"provider_one": "Proveedor API"},
    )

    assert page == 0
    assert page_count == 4
    assert f"1-{_ADMIN_PRODUCTS_PAGE_SIZE}" in text
    assert "Stock local: <b>15</b>" in text
    assert "API: <b>105</b>" in text

    buttons = [button for row in markup.inline_keyboard for button in row]
    assert len(buttons) < 100
    assert (
        sum((button.callback_data or "").startswith("admin:product:") for button in buttons)
        == _ADMIN_PRODUCTS_PAGE_SIZE
    )
    assert any(button.callback_data == "admin:products:1" for button in buttons)

    # Local products are intentionally listed first to simplify stock loading.
    first_callback = markup.inline_keyboard[0][0].callback_data
    assert first_callback == "admin:product:15:0"


def test_admin_product_page_is_clamped_and_keeps_navigation_safe() -> None:
    products = [_product_item(index, external=True) for index in range(1, 102)]

    text, markup, page, page_count = _build_admin_products_page(
        products,
        page=999,
        provider_names={"provider_one": "Proveedor API"},
    )

    assert page == page_count - 1
    assert f"Página <b>{page_count}/{page_count}</b>" in text
    buttons = [button for row in markup.inline_keyboard for button in row]
    assert len(buttons) < 100
    assert any(button.callback_data == f"admin:products:{page - 1}" for button in buttons)
    assert not any(button.callback_data == f"admin:products:{page + 1}" for button in buttons)
