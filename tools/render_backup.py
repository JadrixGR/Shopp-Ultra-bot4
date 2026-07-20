from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import zipfile
from datetime import UTC, datetime
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def copy_database(source: Path, destination: Path) -> None:
    source_connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=30)
    destination_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(destination_connection)
        result = destination_connection.execute("PRAGMA integrity_check").fetchone()
        if result is None or str(result[0]).lower() != "ok":
            raise RuntimeError(f"La copia no superó integrity_check: {result}")
    finally:
        destination_connection.close()
        source_connection.close()


def main() -> int:
    data_dir = Path(os.getenv("RENDER_DATA_DIR", "/var/data")).resolve()
    source_database = data_dir / "shop.db"
    source_providers = data_dir / "providers.json"
    if not source_database.exists():
        print(f"ERROR: no existe {source_database}")
        return 1

    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    backup_dir = data_dir / "backups" / "manual"
    staging = backup_dir / f"staging-{stamp}"
    backup_dir.mkdir(parents=True, exist_ok=True)
    staging.mkdir(parents=True, exist_ok=False)

    try:
        database_copy = staging / "shop.db"
        copy_database(source_database, database_copy)
        files = [database_copy]
        if source_providers.exists():
            providers_copy = staging / "providers.json"
            shutil.copy2(source_providers, providers_copy)
            files.append(providers_copy)

        manifest = {
            "created_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
            "files": [
                {
                    "name": path.name,
                    "size": path.stat().st_size,
                    "sha256": sha256(path),
                }
                for path in files
            ],
        }
        manifest_path = staging / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        files.append(manifest_path)

        archive = backup_dir / f"shop-ultra-{stamp}.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
            for path in files:
                output.write(path, arcname=path.name)

        latest = backup_dir / "shop-ultra-latest.zip"
        latest_temp = backup_dir / ".shop-ultra-latest.zip.tmp"
        shutil.copy2(archive, latest_temp)
        os.replace(latest_temp, latest)
        print(f"Respaldo creado: {archive}")
        print(f"Copia latest: {latest}")
        print(f"SHA-256: {sha256(archive)}")
        return 0
    finally:
        shutil.rmtree(staging, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}")
        raise SystemExit(1) from exc
