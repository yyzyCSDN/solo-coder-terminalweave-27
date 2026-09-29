from __future__ import annotations
from dataclasses import fields, is_dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping
from .clock import iso_z

def to_primitive(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return iso_z(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (Mapping, MappingProxyType)):
        return {str(k): to_primitive(v) for k, v in value.items()}
    if is_dataclass(value):
        return {f.name: to_primitive(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [to_primitive(v) for v in value]
    return str(value)
