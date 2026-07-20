from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import sys
import zipfile
from datetime import datetime
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "MIGRACION_RENDER"
EXCLUDED_ENV_KEYS = {
    "DATABASE_URL",
    "API_PROVIDERS_FILE",
    "PYTHON_VERSION",
    "PYTHONUNBUFFERED",
    "RENDER_DATA_DIR",
    "RENDER_IMPORT_POLL_SECONDS",
    "ALLOW_EMPTY_DATABASE",
    "PORT",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def table_exists(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
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
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    try:
        integrity = str(scalar(connection, "PRAGMA integrity_check", "error"))
        if integrity.lower() != "ok":
            raise RuntimeError(f"SQLite integrity_check devolvió: {integrity}")

        required = ["users", "products", "orders", "deposits"]
        missing = [table for table in required if not table_exists(connection, table)]
        if missing:
            raise RuntimeError("Faltan tablas requeridas: " + ", ".join(missing))

        return {
            "integrity": integrity,
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


def copy_sqlite_database(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=60)
    destination_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(destination_connection)
        integrity = destination_connection.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or str(integrity[0]).lower() != "ok":
            raise RuntimeError(f"La copia no superó integrity_check: {integrity}")
    finally:
        destination_connection.close()
        source_connection.close()


def parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values

    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in EXCLUDED_ENV_KEYS:
            continue
        values[key] = value.strip()
    return values


def write_render_env(source_env: Path, destination: Path) -> int:
    values = parse_env(source_env)
    values["DATABASE_URL"] = "sqlite+aiosqlite:////var/data/shop.db"
    values["API_PROVIDERS_FILE"] = "/var/data/providers.json"
    values["PYTHON_VERSION"] = "3.12.11"
    values["PYTHONUNBUFFERED"] = "1"
    values["RENDER_DATA_DIR"] = "/var/data"
    values["ALLOW_EMPTY_DATABASE"] = "false"

    lines = [f"{key}={value}" for key, value in values.items()]
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(values)


def rotate_output_directory() -> None:
    if not OUTPUT_DIR.exists():
        return
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    rotated = ROOT / f"MIGRACION_RENDER_ANTERIOR_{stamp}"
    OUTPUT_DIR.replace(rotated)
    print(f"La migración anterior fue movida a: {rotated}")


def main() -> int:
    if len(sys.argv) >= 2:
        old_root = Path(sys.argv[1].strip('"')).expanduser().resolve()
    else:
        raw = input("Ruta de la carpeta del bot anterior: ").strip().strip('"')
        old_root = Path(raw).expanduser().resolve()

    source_database = old_root / "data" / "shop.db"
    source_providers = old_root / "data" / "providers.json"
    source_env = old_root / ".env"

    if not old_root.is_dir():
        print(f"ERROR: no existe la carpeta: {old_root}")
        return 1
    if not source_database.exists():
        print(f"ERROR: no existe la base de datos: {source_database}")
        return 1
    if not source_env.exists():
        print(f"ERROR: no existe el archivo de configuración: {source_env}")
        return 1

    rotate_output_directory()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=False)

    copied_database = OUTPUT_DIR / "shop.db"
    copy_sqlite_database(source_database, copied_database)
    source_stats = database_summary(source_database)
    copied_stats = database_summary(copied_database)
    if source_stats != copied_stats:
        raise RuntimeError(
            "La copia no coincide con la base de origen. No se generará el paquete de migración."
        )

    files_for_zip = [copied_database]
    if source_providers.exists():
        copied_providers = OUTPUT_DIR / "providers.json"
        shutil.copy2(source_providers, copied_providers)
        payload = json.loads(copied_providers.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or not isinstance(payload.get("providers", []), list):
            raise RuntimeError("providers.json no contiene el formato esperado")
        files_for_zip.append(copied_providers)

    env_count = write_render_env(source_env, OUTPUT_DIR / ".env.render")

    manifest = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "source_root": str(old_root),
        "database_summary": copied_stats,
        "files": [
            {
                "name": path.name,
                "size": path.stat().st_size,
                "sha256": sha256(path),
            }
            for path in files_for_zip
        ],
    }
    manifest_path = OUTPUT_DIR / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    files_for_zip.append(manifest_path)

    archive_path = OUTPUT_DIR / "import_once.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files_for_zip:
            archive.write(path, arcname=path.name)

    instructions = f"""MIGRACIÓN PREPARADA PARA RENDER
================================

Origen:
{old_root}

Archivos generados:
- .env.render: súbelo en Render mediante Environment > Add from .env.
- import_once.zip: transfiérelo al disco persistente con SUBIR_DATA_A_RENDER.bat.
- manifest.json: resumen y huellas SHA-256.

Datos verificados:
- Usuarios: {copied_stats["users"]}
- Saldo total: {copied_stats["total_balance"]} USDT
- Productos: {copied_stats["products"]} (activos: {copied_stats["active_products"]})
- Stock local disponible: {copied_stats["available_stock"]}
- Compras/historial: {copied_stats["orders"]}
- Depósitos: {copied_stats["deposits"]}
- Integridad SQLite: {copied_stats["integrity"]}
- Variables de entorno preparadas: {env_count}

SEGURIDAD
- No subas la carpeta MIGRACION_RENDER a GitHub.
- No compartas .env.render, providers.json ni import_once.zip.
- El .gitignore del proyecto excluye esta carpeta automáticamente.
"""
    (OUTPUT_DIR / "INSTRUCCIONES.txt").write_text(instructions, encoding="utf-8")

    print("============================================================")
    print("MIGRACIÓN PARA RENDER GENERADA Y VERIFICADA")
    print("============================================================")
    print(f"Carpeta: {OUTPUT_DIR}")
    print(f"Usuarios: {copied_stats['users']}")
    print(f"Saldo total: {copied_stats['total_balance']} USDT")
    print(f"Productos: {copied_stats['products']}")
    print(f"Stock disponible: {copied_stats['available_stock']}")
    print(f"Compras/historial: {copied_stats['orders']}")
    print(f"Depósitos: {copied_stats['deposits']}")
    print(f"ZIP de datos: {archive_path}")
    print(f"SHA-256: {sha256(archive_path)}")
    print("============================================================")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}")
        raise SystemExit(1) from exc
