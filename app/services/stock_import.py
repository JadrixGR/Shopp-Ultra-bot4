from __future__ import annotations

from aiogram import Bot
from aiogram.types import Message

from app.services.catalog import split_stock_payloads

MAX_STOCK_FILE_BYTES = 2 * 1024 * 1024
MAX_STOCK_ITEMS_PER_IMPORT = 20_000
MAX_STOCK_ITEM_LENGTH = 3_500


class StockImportError(ValueError):
    pass


def _decode_text_file(payload: bytes) -> str:
    if b"\x00" in payload:
        raise StockImportError("El archivo no parece ser texto.")
    try:
        return payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            return payload.decode("cp1252")
        except UnicodeDecodeError as exc:
            raise StockImportError("El archivo debe estar codificado como UTF-8.") from exc


def validate_stock_payloads(items: list[str]) -> list[str]:
    if not items:
        raise StockImportError(
            "No se detectaron elementos. Usa uno por línea o separa bloques multilínea con una línea que contenga --."
        )
    if len(items) > MAX_STOCK_ITEMS_PER_IMPORT:
        raise StockImportError(
            f"El máximo por importación es {MAX_STOCK_ITEMS_PER_IMPORT:,} elementos."
        )
    if any(len(item) > MAX_STOCK_ITEM_LENGTH for item in items):
        raise StockImportError(
            f"Cada elemento debe tener menos de {MAX_STOCK_ITEM_LENGTH} caracteres."
        )
    return items


async def extract_stock_payloads(message: Message, bot: Bot) -> list[str]:
    """Read bulk stock from a multiline message or a small text document."""

    if message.text:
        return validate_stock_payloads(split_stock_payloads(message.text))

    document = message.document
    if document is None:
        raise StockImportError(
            "Envía texto con un elemento por línea, bloques separados por --, o adjunta un archivo .txt."
        )
    if document.file_size is not None and document.file_size > MAX_STOCK_FILE_BYTES:
        raise StockImportError("El archivo supera el máximo de 2 MB.")

    filename = (document.file_name or "").lower()
    mime_type = (document.mime_type or "").lower()
    if (
        filename
        and not filename.endswith((".txt", ".csv", ".log"))
        and not mime_type.startswith("text/")
    ):
        raise StockImportError("Adjunta un archivo de texto .txt, .csv o .log.")

    downloaded = await bot.download(document)
    if downloaded is None:
        raise StockImportError("Telegram no pudo descargar el archivo.")
    raw = downloaded.read()
    if len(raw) > MAX_STOCK_FILE_BYTES:
        raise StockImportError("El archivo supera el máximo de 2 MB.")

    return validate_stock_payloads(split_stock_payloads(_decode_text_file(raw)))
