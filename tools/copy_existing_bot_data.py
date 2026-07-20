from __future__ import annotations

import json
import shutil
import sqlite3
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIRED_TABLES = {"users", "products", "orders", "deposits"}


def table_exists(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def scalar(connection: sqlite3.Connection, query: str, default: object = 0) -> object:
    try:
        row = connection.execute(query).fetchone()
    except sqlite3.Error:
        return default
    if row is None or row[0] is None:
        return default
    return row[0]


def database_summary(path: Path) -> dict[str, int | str]:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=60)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or str(integrity[0]).lower() != "ok":
            raise RuntimeError(f"SQLite integrity_check devolvió: {integrity}")

        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        missing = sorted(REQUIRED_TABLES - tables)
        if missing:
            raise RuntimeError("Faltan tablas requeridas: " + ", ".join(missing))

        return {
            "integrity": "ok",
            "users": int(scalar(connection, "SELECT COUNT(*) FROM users")),
            "products": int(scalar(connection, "SELECT COUNT(*) FROM products")),
            "active_products": int(
                scalar(connection, "SELECT COUNT(*) FROM products WHERE active = 1")
            ),
            "orders": int(scalar(connection, "SELECT COUNT(*) FROM orders")),
            "deposits": int(scalar(connection, "SELECT COUNT(*) FROM deposits")),
            "available_stock": int(
                scalar(
                    connection,
                    "SELECT COUNT(*) FROM stock_items WHERE status = 'available'",
                )
                if table_exists(connection, "stock_items")
                else 0
            ),
            "total_balance": str(
                Decimal(str(scalar(connection, "SELECT COALESCE(SUM(balance), 0) FROM users")))
            ),
        }
    finally:
        connection.close()


def copy_sqlite(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_suffix(".db.copying")
    temp.unlink(missing_ok=True)

    source_connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=60)
    destination_connection = sqlite3.connect(temp)
    try:
        source_connection.backup(destination_connection)
        result = destination_connection.execute("PRAGMA integrity_check").fetchone()
        if result is None or str(result[0]).lower() != "ok":
            raise RuntimeError(f"La copia no superó integrity_check: {result}")
    finally:
        destination_connection.close()
        source_connection.close()

    temp.replace(destination)


def backup_existing_destination() -> Path | None:
    candidates = [ROOT / ".env", ROOT / "data" / "shop.db", ROOT / "data" / "providers.json"]
    existing = [path for path in candidates if path.exists()]
    if not existing:
        return None

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = ROOT / f"RESPALDO_LOCAL_ANTES_DE_COPIAR_{stamp}"
    backup_dir.mkdir(parents=True, exist_ok=False)
    for path in existing:
        relative = path.relative_to(ROOT)
        target = backup_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if path.name == "shop.db":
            copy_sqlite(path, target)
        else:
            shutil.copy2(path, target)
    return backup_dir


def validate_providers(path: Path) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("providers", []), list):
        raise RuntimeError("providers.json no contiene el formato esperado")


def main() -> int:
    if len(sys.argv) >= 2:
        old_root = Path(sys.argv[1].strip('"')).expanduser().resolve()
    else:
        raw = input("Ruta de la carpeta del bot anterior: ").strip().strip('"')
        old_root = Path(raw).expanduser().resolve()

    if old_root == ROOT:
        print("ERROR: la carpeta de origen y la carpeta nueva son la misma.")
        return 1
    if not old_root.is_dir():
        print(f"ERROR: no existe la carpeta: {old_root}")
        return 1

    source_env = old_root / ".env"
    source_database = old_root / "data" / "shop.db"
    source_providers = old_root / "data" / "providers.json"

    if not source_env.exists():
        print(f"ERROR: no existe {source_env}")
        return 1
    if not source_database.exists():
        print(f"ERROR: no existe {source_database}")
        return 1

    destination_backup = backup_existing_destination()
    if destination_backup:
        print(f"Respaldo de datos que ya existían en la carpeta nueva: {destination_backup}")

    destination_data = ROOT / "data"
    destination_data.mkdir(parents=True, exist_ok=True)

    source_stats = database_summary(source_database)
    destination_database = destination_data / "shop.db"
    copy_sqlite(source_database, destination_database)
    destination_stats = database_summary(destination_database)
    if source_stats != destination_stats:
        raise RuntimeError("La base copiada no coincide con la base original")

    shutil.copy2(source_env, ROOT / ".env")
    if source_providers.exists():
        validate_providers(source_providers)
        shutil.copy2(source_providers, destination_data / "providers.json")

    # Los archivos WAL/SHM no se copian: sqlite3.backup consolida una base coherente.
    (destination_data / "shop.db-wal").unlink(missing_ok=True)
    (destination_data / "shop.db-shm").unlink(missing_ok=True)

    summary_path = ROOT / "DATOS_COPIADOS_RESUMEN.txt"
    summary_path.write_text(
        "\n".join(
            [
                "DATOS COPIADOS Y VERIFICADOS",
                "===========================",
                f"Origen: {old_root}",
                f"Usuarios: {destination_stats['users']}",
                f"Saldo total: {destination_stats['total_balance']} USDT",
                f"Productos: {destination_stats['products']}",
                f"Productos activos: {destination_stats['active_products']}",
                f"Stock local disponible: {destination_stats['available_stock']}",
                f"Compras/historial: {destination_stats['orders']}",
                f"Depósitos: {destination_stats['deposits']}",
                "Integridad SQLite: ok",
                "",
                "IMPORTANTE:",
                "- .env y data/ son privados y están excluidos por .gitignore.",
                "- No los subas manualmente desde la web de GitHub.",
                "- Usa PUBLICAR_EN_GITHUB.bat para publicar solamente el código.",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    print("============================================================")
    print("DATOS DEL BOT ANTERIOR COPIADOS Y VERIFICADOS")
    print("============================================================")
    print(f"Usuarios: {destination_stats['users']}")
    print(f"Saldo total: {destination_stats['total_balance']} USDT")
    print(f"Productos: {destination_stats['products']}")
    print(f"Stock disponible: {destination_stats['available_stock']}")
    print(f"Compras/historial: {destination_stats['orders']}")
    print(f"Depósitos: {destination_stats['deposits']}")
    print("Integridad SQLite: ok")
    print("============================================================")
    print("No subas .env ni data/ mediante el navegador de GitHub.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}")
        raise SystemExit(1) from exc
