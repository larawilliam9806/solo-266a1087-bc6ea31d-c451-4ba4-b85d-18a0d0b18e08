"""Memory quantity parsing and formatting helpers."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from typing import Any

_MISSING = object()
_MEMORY_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z]*)\s*$")

# Values are bytes. Kubernetes uses binary suffixes (Mi, Gi, ...) and
# decimal suffixes (M, G, ...).  ``m`` as millibytes is not meaningful for
# memory requests, so it is treated as megabytes here.
_UNIT_FACTORS = {
    "b": 1,
    "byte": 1,
    "bytes": 1,
    "k": 1000,
    "kb": 1000,
    "k": 1000,
    "ki": 1024,
    "kib": 1024,
    "m": 1000**2,
    "mb": 1000**2,
    "mi": 1024**2,
    "mib": 1024**2,
    "g": 1000**3,
    "gb": 1000**3,
    "gi": 1024**3,
    "gib": 1024**3,
    "t": 1000**4,
    "tb": 1000**4,
    "ti": 1024**4,
    "tib": 1024**4,
}


class MemoryParseError(ValueError):
    """Raised when a memory quantity cannot be interpreted."""


def _decimal_bytes_to_int(value: Decimal) -> int:
    """Convert an exact Decimal byte count to a non-negative int.

    Memory accounting is performed in integral bytes.  JSON numbers with
    sub-MiB decimals usually originate from an exact byte count rendered as a
    fractional MiB value and may have passed through a binary float round
    trip, leaving an error far below one byte.  Snap such values back to the
    nearest integer instead of truncating a value that is, for example,
    ``B - 1e-7`` to ``B - 1``.
    """

    if value < 0:
        raise MemoryParseError("内存值不能为负数")
    nearest = int(value.to_integral_value(rounding=ROUND_HALF_EVEN))
    tolerance = Decimal("0.01") + abs(value) * Decimal("1e-14")
    if abs(value - Decimal(nearest)) <= tolerance:
        return nearest
    return int(value)


def parse_memory_bytes(value: Any, *, default_unit: str = "MiB", field: str = "memory") -> int:
    """Parse a memory value into an integral number of bytes.

    Plain numbers and numeric strings in the documented snapshot schema are
    interpreted as MiB. Kubernetes-style suffixed strings such as ``512Mi``
    and ``2Gi`` are also accepted.
    """

    if value is None or value is _MISSING:
        return 0

    if isinstance(value, bool):
        raise MemoryParseError(f"{field} 不能是布尔值")

    if isinstance(value, int):
        if value < 0:
            raise MemoryParseError(f"{field} 不能为负数")
        unit = default_unit.lower()
        if unit in {"b", "byte", "bytes"}:
            return value
        return value * _UNIT_FACTORS[unit]

    if isinstance(value, float):
        if value < 0 or value != value:  # NaN check without math import
            raise MemoryParseError(f"{field} 必须是非负数字")
        unit = default_unit.lower()
        factor = 1 if unit in {"b", "byte", "bytes"} else _UNIT_FACTORS[unit]
        return _decimal_bytes_to_int(Decimal(str(value)) * Decimal(factor))

    if isinstance(value, Decimal):
        unit = default_unit.lower()
        factor = 1 if unit in {"b", "byte", "bytes"} else _UNIT_FACTORS[unit]
        return _decimal_bytes_to_int(value * Decimal(factor))

    if not isinstance(value, str):
        raise MemoryParseError(f"{field} 必须是数字或 Kubernetes 风格内存字符串")

    text = value.strip().replace(",", "")
    if not text:
        return 0

    match = _MEMORY_RE.match(text)
    if not match:
        raise MemoryParseError(f"无法解析内存值 {value!r}（字段 {field}）")

    number_text, suffix = match.groups()
    suffix_key = suffix.lower()
    if not suffix_key:
        suffix_key = default_unit.lower()

    factor = _UNIT_FACTORS.get(suffix_key)
    if factor is None:
        raise MemoryParseError(f"不支持的内存单位 {suffix!r}（字段 {field}）")

    try:
        quantity = Decimal(number_text)
    except InvalidOperation as exc:
        raise MemoryParseError(f"无法解析内存值 {value!r}（字段 {field}）") from exc

    bytes_value = quantity * Decimal(factor)
    if bytes_value < 0:
        raise MemoryParseError(f"{field} 不能为负数")
    return _decimal_bytes_to_int(bytes_value)


def bytes_to_mib(value_bytes: int) -> int | float:
    """Convert bytes to MiB, returning an int for integral values."""

    mib = round(value_bytes / float(1024**2), 3)
    if abs(mib - round(mib)) < 0.0005:
        return int(round(mib))
    return mib


def format_mib(value_bytes: int) -> str:
    mib = bytes_to_mib(value_bytes)
    if abs(mib - round(mib)) < 0.0005:
        return str(int(round(mib)))
    return f"{mib:.2f}".rstrip("0").rstrip(".")


def exact_mib_decimal(value_bytes: int) -> Decimal:
    """Return the exact MiB value (bytes / 2**20) as a Decimal."""

    return Decimal(int(value_bytes)) / Decimal(1024**2)
