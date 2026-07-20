from __future__ import annotations

import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

from render_entrypoint import restore_import_archive
from tools.prepare_render_migration import database_summary, write_render_env


def create_legacy_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY,
                telegram_id INTEGER NOT NULL,
                balance NUMERIC NOT NULL DEFAULT 0
            );
            CREATE TABLE products (
                id INTEGER PRIMARY KEY,
                active INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'completed'
            );
            CREATE TABLE deposits (
                id INTEGER PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'credited'
            );
            CREATE TABLE stock_items (
                id INTEGER PRIMARY KEY,
                status TEXT NOT NULL
            );
            INSERT INTO users(id, telegram_id, balance) VALUES (1, 123, 19.25);
            INSERT INTO products(id, active) VALUES (1, 1), (2, 0);
            INSERT INTO orders(id, status) VALUES (1, 'completed');
            INSERT INTO deposits(id, status) VALUES (1, 'credited');
            INSERT INTO stock_items(id, status) VALUES (1, 'available');
            """
        )
        connection.commit()
    finally:
        connection.close()


def test_restore_render_archive(tmp_path: Path) -> None:
    source_db = tmp_path / "source.db"
    create_legacy_database(source_db)
    providers = tmp_path / "providers.json"
    providers.write_text('{"version": 1, "providers": []}\n', encoding="utf-8")

    data_dir = tmp_path / "render-data"
    data_dir.mkdir()
    archive_path = data_dir / "import_once.zip"
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.write(source_db, "shop.db")
        archive.write(providers, "providers.json")

    stats = restore_import_archive(data_dir, archive_path)

    assert stats["users"] == 1
    assert stats["products"] == 2
    assert stats["orders"] == 1
    assert (data_dir / "shop.db").exists()
    assert (data_dir / "providers.json").exists()
    assert not archive_path.exists()
    assert list((data_dir / "imports").glob("imported-*.zip"))
    assert database_summary(data_dir / "shop.db")["total_balance"] == "19.25"


def test_restore_rejects_unsafe_zip_member(tmp_path: Path) -> None:
    data_dir = tmp_path / "render-data"
    data_dir.mkdir()
    archive_path = data_dir / "import_once.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("../shop.db", b"not-a-database")

    with pytest.raises(RuntimeError, match="Ruta insegura"):
        restore_import_archive(data_dir, archive_path)


def test_render_env_excludes_local_paths(tmp_path: Path) -> None:
    source = tmp_path / ".env"
    source.write_text(
        "BOT_TOKEN=test-token\n"
        "ADMIN_IDS=123\n"
        "DATABASE_URL=sqlite+aiosqlite:///./data/shop.db\n"
        "API_PROVIDERS_FILE=data/providers.json\n",
        encoding="utf-8",
    )
    destination = tmp_path / ".env.render"

    count = write_render_env(source, destination)
    rendered = destination.read_text(encoding="utf-8")

    assert count >= 8
    assert "BOT_TOKEN=test-token" in rendered
    assert "DATABASE_URL=sqlite+aiosqlite:////var/data/shop.db" in rendered
    assert "API_PROVIDERS_FILE=/var/data/providers.json" in rendered
    assert "sqlite+aiosqlite:///./data/shop.db" not in rendered


def test_providers_file_is_json(tmp_path: Path) -> None:
    payload = {"version": 1, "providers": []}
    path = tmp_path / "providers.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert json.loads(path.read_text(encoding="utf-8"))["providers"] == []
