from __future__ import annotations

import textwrap
from typing import Any


KEY_ORDER = [
    "id",
    "display-name",
    "homepage",
    "repo",
    "docs",
    "config-file-location",
    "credentials-file-location",
    "history",
    "related-packages",
    "category",
    "package-manager",
    "package-manager-match",
    "package-manager-url",
    "version",
    "license",
    "tags",
    "description",
    "executables",
    "applications",
    "geiger",
    "provenance",
]


def ordered_keys(value: dict[str, Any]) -> list[str]:
    preferred = [key for key in KEY_ORDER if key in value]
    rest = sorted(key for key in value if key not in KEY_ORDER)
    return preferred + rest


def yaml_text(value: dict[str, Any]) -> str:
    lines: list[str] = []
    emit_mapping(lines, value, 0, ordered=True)
    return "\n".join(lines).rstrip() + "\n"


def emit_mapping(lines: list[str], value: dict[str, Any], indent: int, *, ordered: bool = False, parent_key: str | None = None) -> None:
    keys = ordered_keys(value) if ordered else sorted(value)
    prefix = " " * indent
    for key in keys:
        child = value[key]
        if child is None:
            if should_emit_null(key, parent_key):
                lines.append(f"{prefix}{key}: null")
            continue
        if key == "related-packages" and child == []:
            lines.append(f"{prefix}{key}: []")
            continue
        if child == [] or child == {} or child == "":
            continue
        if isinstance(child, dict):
            lines.append(f"{prefix}{key}:")
            emit_mapping(lines, child, indent + 2, parent_key=key)
        elif isinstance(child, list):
            lines.append(f"{prefix}{key}:")
            emit_list(lines, child, indent + 2)
        elif isinstance(child, bool):
            lines.append(f"{prefix}{key}: {'true' if child else 'false'}")
        elif isinstance(child, (int, float)):
            lines.append(f"{prefix}{key}: {child}")
        else:
            emit_scalar(lines, key, str(child), indent)


def emit_list(lines: list[str], value: list[Any], indent: int) -> None:
    prefix = " " * indent
    for child in value:
        if isinstance(child, dict):
            emit_list_mapping(lines, child, indent)
        elif isinstance(child, list):
            lines.append(f"{prefix}-")
            emit_list(lines, child, indent + 2)
        else:
            text = str(child)
            if needs_multiline(text):
                lines.append(f"{prefix}- >")
                emit_wrapped(lines, text, indent + 2)
            else:
                lines.append(f"{prefix}- {quote_scalar(text)}")


def emit_list_mapping(lines: list[str], value: dict[str, Any], indent: int) -> None:
    keys = sorted(key for key in value if value[key] not in (None, [], {}, ""))
    if not keys:
        return
    prefix = " " * indent
    first, rest = keys[0], keys[1:]
    child = value[first]
    if isinstance(child, dict):
        lines.append(f"{prefix}- {first}:")
        emit_mapping(lines, child, indent + 4, parent_key=first)
    elif isinstance(child, list):
        lines.append(f"{prefix}- {first}:")
        emit_list(lines, child, indent + 4)
    elif isinstance(child, bool):
        lines.append(f"{prefix}- {first}: {'true' if child else 'false'}")
    elif isinstance(child, (int, float)):
        lines.append(f"{prefix}- {first}: {child}")
    elif needs_multiline(str(child)):
        lines.append(f"{prefix}- {first}: >")
        emit_wrapped(lines, str(child), indent + 4)
    else:
        lines.append(f"{prefix}- {first}: {quote_scalar(str(child))}")
    for key in rest:
        child = value[key]
        if isinstance(child, dict):
            lines.append(f"{' ' * (indent + 2)}{key}:")
            emit_mapping(lines, child, indent + 4, parent_key=key)
        elif isinstance(child, list):
            lines.append(f"{' ' * (indent + 2)}{key}:")
            emit_list(lines, child, indent + 4)
        elif isinstance(child, bool):
            lines.append(f"{' ' * (indent + 2)}{key}: {'true' if child else 'false'}")
        elif isinstance(child, (int, float)):
            lines.append(f"{' ' * (indent + 2)}{key}: {child}")
        else:
            emit_scalar(lines, key, str(child), indent + 2)


def emit_scalar(lines: list[str], key: str, value: str, indent: int) -> None:
    prefix = " " * indent
    if needs_multiline(value):
        lines.append(f"{prefix}{key}: >")
        emit_wrapped(lines, value, indent + 2)
    else:
        lines.append(f"{prefix}{key}: {quote_scalar(value)}")


def emit_wrapped(lines: list[str], value: str, indent: int) -> None:
    text = " ".join(value.split())
    width = max(40, 88 - indent)
    for line in textwrap.wrap(text, width=width, break_long_words=False, break_on_hyphens=False):
        lines.append(f"{' ' * indent}{line}")


def needs_multiline(value: str) -> bool:
    return "\n" in value or len(value) > 88


def should_emit_null(key: str, parent_key: str | None = None) -> bool:
    return key in {"repo", "config-file-location", "credentials-file-location", "history"} or parent_key == "credentials-file-location"


def quote_scalar(value: str) -> str:
    if value == "":
        return "''"
    lowered = value.lower()
    if lowered in {"true", "false", "null", "yes", "no", "on", "off"}:
        return quote_single(value)
    if value[0] in "-?:!@#&*{}[],|>'\"%" or value.strip() != value or ": " in value:
        return quote_single(value)
    return value


def quote_single(value: str) -> str:
    if "'" in value and '"' not in value and "\\" not in value:
        return '"' + value + '"'
    return "'" + value.replace("'", "''") + "'"
