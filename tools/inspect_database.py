from __future__ import annotations

import sqlite3
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "shop.db"


def scalar(connection: sqlite3.Connection, query: str, default: object = 0) -> object:
    try:
        row = connection.execute(query).fetchone()
    except sqlite3.Error:
        return default
    if row is None or row[0] is None:
        return default
    return row[0]


def table_exists(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def main() -> int:
    db_path = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else DEFAULT_DB
    if not db_path.is_absolute():
        db_path = (ROOT / db_path).resolve()
    if not db_path.exists():
        print(f"ERROR: no existe la base de datos: {db_path}")
        return 1

    try:
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error as exc:
        print(f"ERROR: no se pudo abrir la base de datos: {exc}")
        return 1

    try:
        required = ["users", "products", "orders", "deposits"]
        missing = [table for table in required if not table_exists(connection, table)]
        if missing:
            print("ERROR: la base de datos no tiene las tablas esperadas: " + ", ".join(missing))
            return 1

        users = int(scalar(connection, "SELECT COUNT(*) FROM users"))
        balances = Decimal(str(scalar(connection, "SELECT COALESCE(SUM(balance), 0) FROM users")))
        products = int(scalar(connection, "SELECT COUNT(*) FROM products"))
        active_products = int(scalar(connection, "SELECT COUNT(*) FROM products WHERE active = 1"))
        orders = int(scalar(connection, "SELECT COUNT(*) FROM orders"))
        completed_orders = int(
            scalar(connection, "SELECT COUNT(*) FROM orders WHERE status = 'completed'")
        )
        deposits = int(scalar(connection, "SELECT COUNT(*) FROM deposits"))
        credited_deposits = int(
            scalar(connection, "SELECT COUNT(*) FROM deposits WHERE status = 'credited'")
        )
        available_stock = (
            int(scalar(connection, "SELECT COUNT(*) FROM stock_items WHERE status = 'available'"))
            if table_exists(connection, "stock_items")
            else 0
        )
        provider_purchases = (
            int(scalar(connection, "SELECT COUNT(*) FROM provider_purchases"))
            if table_exists(connection, "provider_purchases")
            else 0
        )
        refunds = (
            int(scalar(connection, "SELECT COUNT(*) FROM refunds"))
            if table_exists(connection, "refunds")
            else 0
        )
        refunded_total = (
            Decimal(str(scalar(connection, "SELECT COALESCE(SUM(amount), 0) FROM refunds")))
            if table_exists(connection, "refunds")
            else Decimal("0")
        )
        adjustments = (
            int(scalar(connection, "SELECT COUNT(*) FROM balance_adjustments"))
            if table_exists(connection, "balance_adjustments")
            else 0
        )
        broadcasts = (
            int(scalar(connection, "SELECT COUNT(*) FROM broadcasts"))
            if table_exists(connection, "broadcasts")
            else 0
        )
        integrity = str(scalar(connection, "PRAGMA integrity_check", "error"))

        print("======================================================")
        print("       DATOS ENCONTRADOS EN SHOP ULTRA BOT")
        print("======================================================")
        print(f"Base de datos: {db_path}")
        print(f"Usuarios: {users}")
        print(f"Saldo total de usuarios: {balances:.2f} USDT")
        print(f"Productos: {products} (activos: {active_products})")
        print(f"Stock local disponible: {available_stock}")
        print(f"Compras/historial: {orders} (completadas: {completed_orders})")
        print(f"Depósitos: {deposits} (acreditados: {credited_deposits})")
        print(f"Pedidos externos registrados: {provider_purchases}")
        print(f"Reembolsos: {refunds} ({refunded_total:.2f} USDT)")
        print(f"Ajustes de saldo: {adjustments}")
        print(f"Anuncios registrados: {broadcasts}")
        print(f"Integridad SQLite: {integrity}")
        print("======================================================")
        return 0 if integrity.lower() == "ok" else 2
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
