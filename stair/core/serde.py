"""Dataclass <-> JSON-safe conversion that keeps Decimal precision and enum types."""
from __future__ import annotations

import functools
import types
import typing
from dataclasses import fields, is_dataclass
from decimal import Decimal
from enum import Enum


def to_plain(obj):
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, Decimal):
        return str(obj)
    if is_dataclass(obj):
        return {f.name: to_plain(getattr(obj, f.name)) for f in fields(obj)}
    if isinstance(obj, dict):
        return {to_plain(k): to_plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [to_plain(v) for v in obj]
    return obj


@functools.cache
def _field_types(cls) -> tuple[tuple[str, object], ...]:
    """Resolved field annotations; get_type_hints re-evaluates strings, so it is done once per class."""
    hints = typing.get_type_hints(cls)
    return tuple((f.name, hints[f.name]) for f in fields(cls))


def from_plain(cls, data: dict):
    return cls(**{name: _coerce(tp, data[name]) for name, tp in _field_types(cls) if name in data})


def _coerce(tp, value):
    if value is None:
        return None
    origin, args = typing.get_origin(tp), typing.get_args(tp)
    if origin in (typing.Union, types.UnionType):
        return _coerce(next(a for a in args if a is not type(None)), value)
    if origin is list:
        return [_coerce(args[0], v) for v in value]
    if origin is dict:
        return {_coerce(args[0], k): _coerce(args[1], v) for k, v in value.items()}
    if tp is Decimal:
        return Decimal(str(value))
    if isinstance(tp, type) and issubclass(tp, Enum):
        return tp(value)
    if is_dataclass(tp):
        return from_plain(tp, value)
    if tp in (int, float, bool, str):
        return tp(value)
    return value
