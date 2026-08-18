"""Shared serializable types used at runtime boundaries."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TypeAlias

JSONScalar: TypeAlias = str | int | float | bool | None
JSONValue: TypeAlias = JSONScalar | Sequence["JSONValue"] | Mapping[str, "JSONValue"]
JSONObject: TypeAlias = Mapping[str, JSONValue]
JSONSchema: TypeAlias = Mapping[str, JSONValue]
