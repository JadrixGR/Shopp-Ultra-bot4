from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

ALLOWED_IMPORT_FILES = {"shop.db", "providers.json", "manifest.json"}
REQUIRED_TABLES = {"users", "products", "orders", "deposits"}
MAX_IMPORT_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "si", "sí"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_sqlite(path: Path) -> dict[str, int | str]:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or str(integrity[0]).lower() != "ok":
            raise RuntimeError(f"La base SQLite no superó integrity_check: {integrity}")

        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        missing = sorted(REQUIRED_TABLES - tables)
        if missing:
            raise RuntimeError(
                "La base importada no contiene las tablas requeridas: " + ", ".join(missing)
            )

        return {
            "integrity": "ok",
            "users": int(connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]),
            "products": int(connection.execute("SELECT COUNT(*) FROM products").fetchone()[0]),
            "orders": int(connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0]),
            "deposits": int(connection.execute("SELECT COUNT(*) FROM deposits").fetchone()[0]),
        }
    finally:
        connection.close()


def _validate_providers(path: Path) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("providers", []), list):
        raise RuntimeError("providers.json no contiene el formato esperado")


def _copy_sqlite_database(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=30)
    destination_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(destination_connection)
        integrity = destination_connection.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or str(integrity[0]).lower() != "ok":
            raise RuntimeError(f"El respaldo previo no superó integrity_check: {integrity}")
    finally:
        destination_connection.close()
        source_connection.close()


def _safe_zip_members(archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    selected: dict[str, zipfile.ZipInfo] = {}
    total_size = 0

    for info in archive.infolist():
        if info.is_dir():
            continue
        posix = PurePosixPath(info.filename)
        if posix.is_absolute() or ".." in posix.parts:
            raise RuntimeError(f"Ruta insegura dentro del ZIP: {info.filename}")

        name = posix.name
        if name not in ALLOWED_IMPORT_FILES:
            raise RuntimeError(f"Archivo no permitido dentro del ZIP: {info.filename}")
        if name in selected:
            raise RuntimeError(f"Archivo duplicado dentro del ZIP: {name}")

        total_size += info.file_size
        if total_size > MAX_IMPORT_UNCOMPRESSED_BYTES:
            raise RuntimeError("El paquete de migración excede el tamaño máximo permitido")
        selected[name] = info

    if "shop.db" not in selected:
        raise RuntimeError("El paquete de migración no contiene shop.db")
    return selected


def restore_import_archive(data_dir: Path, archive_path: Path) -> dict[str, int | str]:
    """Validate and atomically restore a one-time Render migration archive."""

    data_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    database_path = data_dir / "shop.db"
    providers_path = data_dir / "providers.json"

    with tempfile.TemporaryDirectory(prefix=".render-import-", dir=data_dir) as temp_name:
        staging = Path(temp_name)
        with zipfile.ZipFile(archive_path) as archive:
            members = _safe_zip_members(archive)
            for name, info in members.items():
                target = staging / name
                with archive.open(info) as source, target.open("wb") as destination:
                    shutil.copyfileobj(source, destination)

        staged_database = staging / "shop.db"
        stats = _validate_sqlite(staged_database)

        staged_providers = staging / "providers.json"
        if staged_providers.exists():
            _validate_providers(staged_providers)

        backup_dir = data_dir / "backups" / f"pre-import-{stamp}"
        if database_path.exists() or providers_path.exists():
            backup_dir.mkdir(parents=True, exist_ok=False)
            if database_path.exists():
                _copy_sqlite_database(database_path, backup_dir / "shop.db")
            if providers_path.exists():
                shutil.copy2(providers_path, backup_dir / "providers.json")

        database_temp = data_dir / f".shop.db.import-{stamp}"
        shutil.copy2(staged_database, database_temp)
        os.replace(database_temp, database_path)

        if staged_providers.exists():
            providers_temp = data_dir / f".providers.json.import-{stamp}"
            shutil.copy2(staged_providers, providers_temp)
            os.replace(providers_temp, providers_path)

        imported_dir = data_dir / "imports"
        imported_dir.mkdir(parents=True, exist_ok=True)
        archived_copy = imported_dir / f"imported-{stamp}-{_sha256(archive_path)[:12]}.zip"
        os.replace(archive_path, archived_copy)

        receipt = {
            "imported_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
            "archive": archived_copy.name,
            "database_sha256": _sha256(database_path),
            "stats": stats,
        }
        (imported_dir / f"receipt-{stamp}.json").write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    return stats


def wait_for_initial_data(data_dir: Path) -> None:
    database_path = data_dir / "shop.db"
    import_path = data_dir / "import_once.zip"
    allow_empty = _truthy(os.getenv("ALLOW_EMPTY_DATABASE"))
    wait_seconds = max(2, int(os.getenv("RENDER_IMPORT_POLL_SECONDS", "10")))

    data_dir.mkdir(parents=True, exist_ok=True)
    announced = False

    while True:
        if import_path.exists():
            print("[Render] Paquete de migración detectado. Validando e importando...", flush=True)
            stats = restore_import_archive(data_dir, import_path)
            print(
                "[Render] Migración completada: "
                f"usuarios={stats['users']}, productos={stats['products']}, "
                f"compras={stats['orders']}, depósitos={stats['deposits']}",
                flush=True,
            )
            return

        if database_path.exists():
            stats = _validate_sqlite(database_path)
            print(
                "[Render] Base persistente encontrada: "
                f"usuarios={stats['users']}, compras={stats['orders']}",
                flush=True,
            )
            return

        if allow_empty:
            print(
                "[Render] ALLOW_EMPTY_DATABASE=true: se iniciará una base nueva en /var/data.",
                flush=True,
            )
            return

        if not announced:
            print(
                "[Render] El servicio está listo, pero no iniciará Telegram hasta recibir "
                "/var/data/import_once.zip. Esto evita abrir una tienda vacía durante la migración.",
                flush=True,
            )
            announced = True
        time.sleep(wait_seconds)


def main() -> None:
    data_dir = Path(os.getenv("RENDER_DATA_DIR", "/var/data")).expanduser().resolve()
    database_path = data_dir / "shop.db"
    providers_path = data_dir / "providers.json"

    os.environ.setdefault(
        "DATABASE_URL",
        f"sqlite+aiosqlite:////{database_path.as_posix().lstrip('/')}",
    )
    os.environ.setdefault("API_PROVIDERS_FILE", str(providers_path))
    os.environ.setdefault("PYTHONUNBUFFERED", "1")

    wait_for_initial_data(data_dir)
    print("[Render] Iniciando Shop Ultra Bot...", flush=True)
    os.execv(sys.executable, [sys.executable, "-m", "app.main"])


if __name__ == "__main__":
    main()
