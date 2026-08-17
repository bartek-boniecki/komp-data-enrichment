"""HubSpot property schema awareness.

HubSpot silently rejects whole write requests when a dropdown (``enumeration``)
property receives a value outside its option list, so free-text values such as
``"75 - dobry fit"`` never land in fields like ``leads_score``. This module reads
the live property definitions and coerces payloads to values HubSpot accepts,
reporting anything it had to drop.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Iterable

MULTI_VALUE_SEPARATOR = ";"

# HubSpot option labels are hand-typed and sometimes contain typos
# (e.g. "Zarządzanie Odpadamy"), so near-identical labels still match.
_FUZZY_MATCH_RATIO = 0.86

_MULTI_FIELD_TYPES = frozenset({"checkbox"})
_NUMERIC_TYPES = frozenset({"number"})


def normalize_option_key(value: str) -> str:
    """Comparison key ignoring case, diacritics, and punctuation."""
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("ł", "l").replace("Ł", "L")
    text = re.sub(r"[^0-9a-zA-Z]+", " ", text)
    return " ".join(text.lower().split())


@dataclass(frozen=True)
class PropertyDef:
    name: str
    type: str
    field_type: str
    options: tuple[str, ...] = ()
    read_only: bool = False

    @property
    def is_enumeration(self) -> bool:
        return self.type == "enumeration" and bool(self.options)

    @property
    def is_multi_value(self) -> bool:
        return self.field_type in _MULTI_FIELD_TYPES

    def match_option(self, value: str) -> str | None:
        """Best-effort map a value onto one of the allowed options."""
        raw = str(value or "").strip()
        if not raw:
            return None
        for option in self.options:
            if option == raw:
                return option
        key = normalize_option_key(raw)
        if not key:
            return None
        by_key: dict[str, str] = {}
        for option in self.options:
            by_key.setdefault(normalize_option_key(option), option)
        if key in by_key:
            return by_key[key]
        for option_key, option in by_key.items():
            if option_key and (option_key.startswith(key) or key.startswith(option_key)):
                return option
        for option_key, option in by_key.items():
            if option_key and (option_key in key or key in option_key):
                return option
        best: tuple[float, str | None] = (0.0, None)
        for option_key, option in by_key.items():
            ratio = SequenceMatcher(None, key, option_key).ratio()
            if ratio > best[0]:
                best = (ratio, option)
        return best[1] if best[0] >= _FUZZY_MATCH_RATIO else None


@dataclass
class DroppedProperty:
    property_name: str
    value: str
    reason: str

    def as_dict(self) -> dict[str, str]:
        return {
            "property": self.property_name,
            "value": self.value[:120],
            "reason": self.reason,
        }


@dataclass
class PropertySchema:
    """Property definitions for one HubSpot object type."""

    object_type: str
    properties: dict[str, PropertyDef] = field(default_factory=dict)
    available: bool = True

    @classmethod
    def from_api_results(cls, object_type: str, results: Iterable[dict[str, Any]]) -> PropertySchema:
        defs: dict[str, PropertyDef] = {}
        for item in results or ():
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            options = tuple(
                str(opt.get("value"))
                for opt in (item.get("options") or [])
                if opt.get("value") is not None
            )
            metadata = item.get("modificationMetadata") or {}
            defs[name] = PropertyDef(
                name=name,
                type=str(item.get("type") or ""),
                field_type=str(item.get("fieldType") or ""),
                options=options,
                read_only=bool(metadata.get("readOnlyValue")),
            )
        return cls(object_type=object_type, properties=defs)

    @classmethod
    def unavailable(cls, object_type: str) -> PropertySchema:
        return cls(object_type=object_type, properties={}, available=False)

    def coerce(self, props: dict[str, str]) -> tuple[dict[str, str], list[DroppedProperty]]:
        """Return payload HubSpot accepts plus the properties that were dropped."""
        if not self.available or not self.properties:
            return dict(props), []

        out: dict[str, str] = {}
        dropped: list[DroppedProperty] = []
        for name, value in props.items():
            text = "" if value is None else str(value).strip()
            if not text:
                continue
            definition = self.properties.get(name)
            if definition is None:
                dropped.append(
                    DroppedProperty(name, text, f"property does not exist on {self.object_type}")
                )
                continue
            if definition.read_only:
                dropped.append(DroppedProperty(name, text, "property is read-only in HubSpot"))
                continue
            if definition.type in _NUMERIC_TYPES:
                number = _first_number(text)
                if number is None:
                    dropped.append(DroppedProperty(name, text, "expected a number"))
                    continue
                out[name] = number
                continue
            if definition.is_enumeration:
                matched = _match_enum_value(definition, text)
                if not matched:
                    dropped.append(
                        DroppedProperty(
                            name,
                            text,
                            f"no matching option (allowed: {', '.join(definition.options[:8])})",
                        )
                    )
                    continue
                out[name] = matched
                continue
            out[name] = text
        return out, dropped


def _first_number(text: str) -> str | None:
    match = re.search(r"-?\d+(?:[.,]\d+)?", text)
    if not match:
        return None
    return match.group(0).replace(",", ".")


def _match_enum_value(definition: PropertyDef, text: str) -> str | None:
    if not definition.is_multi_value:
        return definition.match_option(text)
    matched: list[str] = []
    for part in text.split(MULTI_VALUE_SEPARATOR):
        option = definition.match_option(part)
        if option and option not in matched:
            matched.append(option)
    return MULTI_VALUE_SEPARATOR.join(matched) if matched else None
