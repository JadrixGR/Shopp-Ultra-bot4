from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
BACKUPS_DIR = ROOT / "backups"
DATABASE = DATA_DIR / "shop.db"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def copy_sqlite_database(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=30)
    destination_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(destination_connection)
        integrity = destination_connection.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or str(integrity[0]).lower() != "ok":
            raise RuntimeError(f"La copia de SQLite no superó integrity_check: {integrity}")
    finally:
        destination_connection.close()
        source_connection.close()


def main() -> int:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    destination = BACKUPS_DIR / stamp
    destination_data = destination / "data"
    destination_data.mkdir(parents=True, exist_ok=False)

    copied: list[Path] = []
    env_path = ROOT / ".env"
    if env_path.exists():
        shutil.copy2(env_path, destination / ".env")
        copied.append(destination / ".env")

    if DATABASE.exists():
        backup_db = destination_data / "shop.db"
        copy_sqlite_database(DATABASE, backup_db)
        copied.append(backup_db)

    if DATA_DIR.exists():
        for source in DATA_DIR.rglob("*"):
            if not source.is_file():
                continue
            if source.name in {"shop.db", "shop.db-wal", "shop.db-shm"}:
                continue
            relative = source.relative_to(DATA_DIR)
            target = destination_data / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            copied.append(target)

    if not copied:
        print("ERROR: no se encontraron .env ni archivos de datos para respaldar.")
        shutil.rmtree(destination, ignore_errors=True)
        return 1

    manifest = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "source_root": str(ROOT),
        "files": [
            {
                "path": str(path.relative_to(destination)),
                "size": path.stat().st_size,
                "sha256": sha256(path),
            }
            for path in sorted(copied)
        ],
    }
    manifest_path = destination / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    print("Respaldo verificado creado en:")
    print(destination)
    print(f"Archivos respaldados: {len(copied)}")
    if DATABASE.exists():
        print("La base shop.db se copió mediante el mecanismo de respaldo de SQLite.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}")
        raise SystemExit(1) from exc
