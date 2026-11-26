"""Precision-preserving JSON helpers for snapshot forms and editing."""

from __future__ import annotations

import json
import re
import secrets
from decimal import Decimal
from typing import Any

from .memory import exact_mib_decimal, parse_memory_bytes

_SENTINEL = "__exactMemoryBytes__"


def _exact_view(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Return a copy whose MiB convenience fields are exact decimal values.

    The canonical snapshot stores exact counts in ``*Bytes``; the rounded
    ``*MiB`` fields are for display only.  When snapshot JSON is embedded in
    forms or in the editor, emit exact MiB decimals so that re-normalization
    reproduces the exact same byte counts even when the explicit byte fields
    are ignored by an older code path.
    """

    view: dict[str, Any] = {
        key: value for key, value in snapshot.items() if key not in ("nodes", "pods")
    }
    nodes = []
    for node in snapshot.get("nodes", []):
        item = dict(node)
        if isinstance(item.get("allocatableBytes"), int):
            item["allocatableMemoryMiB"] = exact_mib_decimal(item["allocatableBytes"])
        nodes.append(item)
    pods = []
    for pod in snapshot.get("pods", []):
        item = dict(pod)
        if isinstance(item.get("memoryBytes"), int):
            item["memoryRequestMiB"] = exact_mib_decimal(item["memoryBytes"])
        pods.append(item)
    view["nodes"] = nodes
    view["pods"] = pods
    return view


def dumps_snapshot(snapshot: dict[str, Any], *, indent: int | None = None) -> str:
    """Serialize a canonical snapshot while preserving exact MiB decimals."""

    view = _exact_view(snapshot)
    nonce = secrets.token_hex(8)
    literals: list[str] = []

    def _default(obj: Any) -> str:
        if isinstance(obj, Decimal):
            token = f"@@RAW{nonce}{len(literals)}@@"
            literals.append(format(obj, "f"))
            return token
        raise TypeError(f"Object of type {type(obj)!r} is not JSON serializable")

    text = json.dumps(view, ensure_ascii=False, indent=indent, default=_default)
    pattern = re.compile(r'"@@RAW' + re.escape(nonce) + r'(\d+)@@"')

    def _sub(match: re.Match[str]) -> str:
        return literals[int(match.group(1))]

    return pattern.sub(_sub, text)


def _reconcile_memory_object(
    raw: dict[str, Any],
    baseline: dict[str, Any],
    *,
    exact_key: str,
    human_key: str,
) -> None:
    baseline_bytes = baseline.get(exact_key)
    edited_bytes = raw.get(exact_key)
    if not isinstance(baseline_bytes, int) or edited_bytes is None:
        return

    try:
        edited_exact = parse_memory_bytes(edited_bytes, default_unit="B", field=exact_key)
    except Exception:
        return

    if edited_exact == baseline_bytes:
        return

    # The explicit byte count changed relative to the validated baseline.
    # Force the exact value; this covers edits made directly to *Bytes in
    # the JSON editor regardless of the stale MiB convenience field.
    raw[_SENTINEL] = edited_exact

    # If the human MiB field was untouched but is its rounded display form,
    # leave it as-is; normalization will use the sentinel.
    human = raw.get(human_key)
    if human is not None:
        try:
            human_bytes = parse_memory_bytes(human, default_unit="MiB", field=human_key)
        except Exception:
            return
        if human_bytes == edited_exact:
            # Both representations agree; drop the sentinel and keep JSON clean.
            raw.pop(_SENTINEL, None)


def reconcile_with_baseline(payload: Any, baseline: dict[str, Any] | None) -> None:
    """Disambiguate dual memory fields after editing the canonical JSON.

    The editor shows both exact ``*Bytes`` and MiB fields.  Comparing the
    submitted object to the validated baseline lets us honor edits made to
    either representation while preserving untouched byte precision.
    """

    if not isinstance(payload, dict) or not isinstance(baseline, dict):
        return

    base_nodes = {
        node.get("name"): node
        for node in baseline.get("nodes", [])
        if isinstance(node, dict) and node.get("name") is not None
    }
    base_pods = {
        pod.get("name"): pod
        for pod in baseline.get("pods", [])
        if isinstance(pod, dict) and pod.get("name") is not None
    }

    nodes = payload.get("nodes")
    if isinstance(nodes, list):
        for raw_node in nodes:
            if not isinstance(raw_node, dict):
                continue
            base = base_nodes.get(raw_node.get("name"))
            if base is not None:
                _reconcile_memory_object(
                    raw_node,
                    base,
                    exact_key="allocatableBytes",
                    human_key="allocatableMemoryMiB",
                )

    pods = payload.get("pods")
    if isinstance(pods, list):
        for raw_pod in pods:
            if not isinstance(raw_pod, dict):
                continue
            base = base_pods.get(raw_pod.get("name"))
            if base is not None:
                _reconcile_memory_object(
                    raw_pod,
                    base,
                    exact_key="memoryBytes",
                    human_key="memoryRequestMiB",
                )
