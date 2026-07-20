from __future__ import annotations

import html
import json
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from aiogram.types import Message

_ALLOWED_ENTITY_TYPES = {
    "bold",
    "italic",
    "underline",
    "strikethrough",
    "spoiler",
    "code",
    "pre",
    "text_link",
    "text_mention",
    "custom_emoji",
    "blockquote",
    "expandable_blockquote",
}


@dataclass(frozen=True, slots=True)
class RichEntity:
    type: str
    start: int
    end: int
    url: str | None = None
    user_id: int | None = None
    language: str | None = None
    custom_emoji_id: str | None = None


def _entity_type_value(value: object) -> str:
    raw = getattr(value, "value", value)
    return str(raw).strip().lower()


def _utf16_to_python_index(text: str, target: int) -> int:
    if target <= 0:
        return 0
    used = 0
    for index, character in enumerate(text):
        if used >= target:
            return index
        used += 2 if ord(character) > 0xFFFF else 1
        if used >= target:
            return index + 1
    return len(text)


def _message_text_and_entities(message: Message) -> tuple[str, list[object]]:
    if message.text is not None:
        return message.text, list(message.entities or [])
    if message.caption is not None:
        return message.caption, list(message.caption_entities or [])
    return "", []


def capture_message_rich_text(message: Message) -> tuple[str, str]:
    """Return trimmed plain text and a portable JSON representation of its entities.

    Telegram entity offsets use UTF-16 code units. They are converted to Python
    character indexes before storage so later rendering and truncation remain
    deterministic across Windows and Linux.
    """

    source, raw_entities = _message_text_and_entities(message)
    left = len(source) - len(source.lstrip())
    right = len(source.rstrip())
    text = source[left:right]
    if not text:
        return "", "[]"

    records: list[dict[str, Any]] = []
    for entity in raw_entities:
        kind = _entity_type_value(getattr(entity, "type", ""))
        if kind not in _ALLOWED_ENTITY_TYPES:
            continue
        offset = int(getattr(entity, "offset", 0) or 0)
        length = int(getattr(entity, "length", 0) or 0)
        start = _utf16_to_python_index(source, offset)
        end = _utf16_to_python_index(source, offset + length)
        clipped_start = max(start, left)
        clipped_end = min(end, right)
        if clipped_start >= clipped_end:
            continue
        if kind == "custom_emoji" and (clipped_start != start or clipped_end != end):
            continue

        record: dict[str, Any] = {
            "type": kind,
            "start": clipped_start - left,
            "end": clipped_end - left,
        }
        url = getattr(entity, "url", None)
        if url:
            record["url"] = str(url)
        language = getattr(entity, "language", None)
        if language:
            record["language"] = str(language)
        custom_id = getattr(entity, "custom_emoji_id", None)
        if custom_id:
            record["custom_emoji_id"] = str(custom_id)
        user = getattr(entity, "user", None)
        user_id = getattr(user, "id", None)
        if user_id is not None:
            record["user_id"] = int(user_id)
        records.append(record)

    return text, json.dumps(records, ensure_ascii=False, separators=(",", ":"))


def _load_entities(serialized: str | None, text_length: int) -> list[RichEntity]:
    if not serialized:
        return []
    try:
        raw = json.loads(serialized)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(raw, list):
        return []

    entities: list[RichEntity] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or "").strip().lower()
        if kind not in _ALLOWED_ENTITY_TYPES:
            continue
        try:
            start = max(0, int(item.get("start", 0)))
            end = min(text_length, int(item.get("end", 0)))
        except (TypeError, ValueError):
            continue
        if start >= end:
            continue
        custom_id = str(item.get("custom_emoji_id") or "").strip() or None
        if kind == "custom_emoji" and not custom_id:
            continue
        user_id_raw = item.get("user_id")
        try:
            user_id = int(user_id_raw) if user_id_raw is not None else None
        except (TypeError, ValueError):
            user_id = None
        entities.append(
            RichEntity(
                type=kind,
                start=start,
                end=end,
                url=str(item.get("url") or "").strip() or None,
                user_id=user_id,
                language=str(item.get("language") or "").strip() or None,
                custom_emoji_id=custom_id,
            )
        )
    return entities


def _entity_tags(entity: RichEntity) -> tuple[str, str]:
    if entity.type == "bold":
        return "<b>", "</b>"
    if entity.type == "italic":
        return "<i>", "</i>"
    if entity.type == "underline":
        return "<u>", "</u>"
    if entity.type == "strikethrough":
        return "<s>", "</s>"
    if entity.type == "spoiler":
        return '<span class="tg-spoiler">', "</span>"
    if entity.type == "code":
        return "<code>", "</code>"
    if entity.type == "pre":
        if entity.language:
            language = html.escape(entity.language, quote=True)
            return f'<pre><code class="language-{language}">', "</code></pre>"
        return "<pre>", "</pre>"
    if entity.type == "text_link" and entity.url:
        return f'<a href="{html.escape(entity.url, quote=True)}">', "</a>"
    if entity.type == "text_mention" and entity.user_id is not None:
        return f'<a href="tg://user?id={entity.user_id}">', "</a>"
    if entity.type == "custom_emoji" and entity.custom_emoji_id:
        emoji_id = html.escape(entity.custom_emoji_id, quote=True)
        return f'<tg-emoji emoji-id="{emoji_id}">', "</tg-emoji>"
    if entity.type == "blockquote":
        return "<blockquote>", "</blockquote>"
    if entity.type == "expandable_blockquote":
        return "<blockquote expandable>", "</blockquote>"
    return "", ""


def render_rich_text(
    text: str | None,
    serialized_entities: str | None,
    *,
    max_chars: int | None = None,
    empty_fallback: str = "—",
) -> str:
    """Render stored Telegram entities as safe HTML, including animated custom emoji."""

    source = str(text or "")
    if not source:
        return html.escape(empty_fallback, quote=False)

    truncated = max_chars is not None and len(source) > max_chars
    visible_length = min(len(source), max_chars) if max_chars is not None else len(source)
    visible = source[:visible_length]
    entities = _load_entities(serialized_entities, len(source))

    clipped: list[RichEntity] = []
    for entity in entities:
        if entity.start >= visible_length:
            continue
        end = min(entity.end, visible_length)
        if entity.type == "custom_emoji" and end != entity.end:
            continue
        if entity.start < end:
            clipped.append(
                RichEntity(
                    type=entity.type,
                    start=entity.start,
                    end=end,
                    url=entity.url,
                    user_id=entity.user_id,
                    language=entity.language,
                    custom_emoji_id=entity.custom_emoji_id,
                )
            )

    # Telegram entities are nested or disjoint. Opening outer ranges first and
    # closing in reverse opening order preserves valid HTML for equal boundaries.
    clipped.sort(key=lambda item: (item.start, -item.end, item.type))
    opens: dict[int, list[tuple[int, str]]] = defaultdict(list)
    closes: dict[int, list[tuple[int, str]]] = defaultdict(list)
    for order, entity in enumerate(clipped):
        open_tag, close_tag = _entity_tags(entity)
        if not open_tag:
            continue
        opens[entity.start].append((order, open_tag))
        closes[entity.end].append((order, close_tag))

    parts: list[str] = []
    for position in range(len(visible) + 1):
        for _order, close_tag in sorted(closes.get(position, []), reverse=True):
            parts.append(close_tag)
        for _order, open_tag in sorted(opens.get(position, [])):
            parts.append(open_tag)
        if position < len(visible):
            parts.append(html.escape(visible[position], quote=False))
    if truncated:
        parts.append("…")
    return "".join(parts)


def has_custom_emoji(serialized_entities: str | None) -> bool:
    return any(
        entity.type == "custom_emoji" for entity in _load_entities(serialized_entities, 10**7)
    )


def ensure_html_block_before(
    text: str,
    block: str,
    *,
    markers: tuple[str, ...],
) -> str:
    """Insert a required HTML block before the first known section marker.

    Older installations can have customized message templates that predate a
    new placeholder. This keeps mandatory delivery instructions visible even
    when such an override is still stored in the customer's database.
    """

    normalized = block.strip()
    if not normalized or normalized in text:
        return text
    for marker in markers:
        position = text.find(marker)
        if position >= 0:
            return text[:position] + block + text[position:]
    return text.rstrip() + "\n\n" + normalized
