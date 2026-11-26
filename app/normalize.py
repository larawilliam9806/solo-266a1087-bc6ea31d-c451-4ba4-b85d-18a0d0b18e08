"""Snapshot normalization and validation."""

from __future__ import annotations

import re
from typing import Any, Iterable

from .memory import MemoryParseError, bytes_to_mib, parse_memory_bytes

MISSING = object()


class SnapshotError(ValueError):
    """Raised when an uploaded snapshot does not satisfy the schema."""


def _dig(data: Any, path: str) -> Any:
    current = data
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return MISSING
    return current


def _first(data: dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        value = _dig(data, key)
        if value is not MISSING and value is not None:
            if isinstance(value, str) and not value.strip():
                continue
            return value
    return MISSING


def _require(value: Any, message: str) -> Any:
    if value is MISSING or value is None or (isinstance(value, str) and not value.strip()):
        raise SnapshotError(message)
    return value


def _string(value: Any) -> str:
    return str(value).strip()


def parse_bool(value: Any, *, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value in (0, 1):
            return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "y", "on", "是", "可中断", "允许"}:
            return True
        if normalized in {"false", "0", "no", "n", "off", "否", "不可中断", "不允许"}:
            return False
    raise SnapshotError(f"{field} 必须是布尔值（true/false）")


_BATCH_TOKENS = (
    "batch",
    "job",
    "cron",
    "offline",
    "train",
    "spark",
    "批",
    "离线",
)
_ONLINE_TOKENS = (
    "online",
    "on-line",
    "service",
    "web",
    "api",
    "front",
    "back",
    "app",
    "deployment",
    "stateful",
    "replica",
    "pod",
    "在线",
    "服务",
)


def classify_workload_type(raw_type: Any, interruptible: bool) -> str:
    """Return canonical ``online`` or ``batch`` workload type."""

    if raw_type is MISSING or raw_type is None:
        return "batch" if interruptible else "online"

    text = str(raw_type).strip().lower()
    if not text:
        return "batch" if interruptible else "online"

    if any(token in text for token in _BATCH_TOKENS):
        return "batch"
    if any(token in text for token in _ONLINE_TOKENS):
        return "online"

    # The schema asks for online/batch. For an unknown custom type, an
    # interruptible workload is treated as a batch candidate; otherwise it is
    # treated as an online workload that must be evacuated and preserved.
    return "batch" if interruptible else "online"


def _extract_node_list(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        kubernetes_nodes = [
            item for item in payload if isinstance(item, dict) and item.get("kind") == "Node"
        ]
        return kubernetes_nodes or payload

    if not isinstance(payload, dict):
        raise SnapshotError("快照顶层必须是 JSON 对象，或节点数组")

    for key in ("nodes", "nodeList", "node_list", "节点", "节点列表"):
        value = payload.get(key)
        if isinstance(value, list):
            kubernetes_nodes = [
                item for item in value if isinstance(item, dict) and item.get("kind") == "Node"
            ]
            return kubernetes_nodes or value
        if isinstance(value, dict):
            # Allow {"node-a": {...}} maps.
            return list(value.values())

    items = payload.get("items")
    if isinstance(items, list):
        kubernetes_nodes = [
            item for item in items if isinstance(item, dict) and item.get("kind") == "Node"
        ]
        if kubernetes_nodes:
            return kubernetes_nodes
        if str(payload.get("kind", "")).endswith("NodeList"):
            return items
        # Generic snapshots may use "items" as their node collection.
        return items

    # Maybe the snapshot is a single node.
    if any(key in payload for key in ("allocatableMemoryMiB", "allocatableMemory", "status", "allocatable")):
        return [payload]

    raise SnapshotError("快照中未找到 nodes 节点列表")


def _extract_pod_list(payload: Any, raw_nodes: list[Any]) -> list[Any]:
    if isinstance(payload, dict):
        for key in (
            "pods",
            "podList",
            "pod_list",
            "workloads",
            "Pod列表",
            "pod列表",
            "Pod",
            "工作负载",
        ):
            value = payload.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                return list(value.values())

        items = payload.get("items")
        if isinstance(items, list):
            kubernetes_pods = [
                item for item in items if isinstance(item, dict) and item.get("kind") == "Pod"
            ]
            if kubernetes_pods:
                return kubernetes_pods
            if str(payload.get("kind", "")).endswith("PodList"):
                return items

    # Pods may be embedded in each node object.
    embedded: list[Any] = []
    for node in raw_nodes:
        if isinstance(node, dict):
            for key in ("pods", "podList", "pod_list"):
                value = node.get(key)
                if isinstance(value, list):
                    embedded.extend(value)
    if embedded:
        return embedded

    if isinstance(payload, list):
        # Mixed Kubernetes objects.
        pods = [item for item in payload if isinstance(item, dict) and item.get("kind") == "Pod"]
        if pods:
            return pods

    raise SnapshotError("快照中未找到 pods Pod 列表，也没有节点内嵌 pods")


def _node_zone(raw: dict[str, Any]) -> str | None:
    value = _first(
        raw,
        (
            "zone",
            "az",
            "azName",
            "availabilityZone",
            "availability_zone",
            "topologyZone",
            "可用区",
            "节点可用区",
            "metadata.labels.topology.kubernetes.io/zone",
            "metadata.labels.failure-domain.beta.kubernetes.io/zone",
        ),
    )
    if value is not MISSING:
        return _string(value) or None

    labels = raw.get("labels")
    if not isinstance(labels, dict):
        metadata_labels = _dig(raw, "metadata.labels")
        if isinstance(metadata_labels, dict):
            labels = metadata_labels
    if isinstance(labels, dict):
        for key in (
            "topology.kubernetes.io/zone",
            "failure-domain.beta.kubernetes.io/zone",
            "zone",
            "az",
        ):
            if labels.get(key):
                return _string(labels[key])
    return None


def _coerce_exact_bytes(value: Any, field: str) -> int:
    """Interpret an explicit byte-count field (ints are already bytes)."""

    if isinstance(value, bool):
        raise SnapshotError(f"{field} 不能是布尔值")
    try:
        return parse_memory_bytes(value, default_unit="B", field=field)
    except MemoryParseError as exc:
        raise SnapshotError(str(exc)) from exc


def _resolve_memory_bytes(
    raw: dict[str, Any],
    *,
    exact_keys: tuple[str, ...],
    human_keys: tuple[str, ...],
    index_label: str,
    missing_message: str,
) -> int:
    """Resolve memory in exact bytes.

    ``exact_keys`` carry an integral byte count produced by this application;
    ``human_keys`` use the documented MiB-default / quantity-string syntax.

    When both are present and disagree, the human-facing field is assumed to
    contain a fresh edit and wins; callers that know a baseline can instead
    set the ``__exactMemoryBytes__`` sentinel to force an edited byte count.
    """

    forced = raw.get("__exactMemoryBytes__")
    if forced is not None:
        return _coerce_exact_bytes(forced, index_label)

    exact_value: Any = MISSING
    exact_key_used: str | None = None
    for key in exact_keys:
        if key in raw and raw[key] is not None:
            exact_value = raw[key]
            exact_key_used = key
            break

    human_value = _first(raw, human_keys)

    if exact_value is MISSING and human_value is MISSING:
        raise SnapshotError(missing_message)

    exact_bytes: int | None = None
    if exact_value is not MISSING:
        exact_bytes = _coerce_exact_bytes(exact_value, f"{index_label}.{exact_key_used}")

    if human_value is MISSING:
        assert exact_bytes is not None
        return exact_bytes

    try:
        human_bytes = parse_memory_bytes(
            human_value, default_unit="MiB", field=index_label
        )
    except MemoryParseError as exc:
        raise SnapshotError(str(exc)) from exc

    if exact_bytes is None:
        return human_bytes

    if human_bytes == exact_bytes:
        return exact_bytes

    # The canonical snapshot emits a rounded MiB *display* value next to the
    # exact byte count.  Re-normalizing that unchanged pair must preserve the
    # exact bytes (a few bytes are invisible in the rounded value).  Only a
    # human value that differs from that rounded representation is treated as
    # a fresh edit and wins over the stale exact field.
    rounded_display_bytes = parse_memory_bytes(
        bytes_to_mib(exact_bytes), default_unit="MiB", field=index_label
    )
    if human_bytes == rounded_display_bytes:
        return exact_bytes

    return human_bytes


def _node_memory(raw: dict[str, Any], index: int) -> int:
    return _resolve_memory_bytes(
        raw,
        exact_keys=("allocatableBytes", "allocatable_bytes"),
        human_keys=(
            "allocatableMemoryMiB",
            "allocatable_memory_mib",
            "allocatableMemoryMB",
            "allocatableMemory",
            "allocatable_memory",
            "memoryMiB",
            "memory_mib",
            "memory",
            "可分配内存",
            "allocatable.memory",
            "status.allocatable.memory",
            "status.capacity.memory",
            "capacity.memory",
        ),
        index_label=f"nodes[{index}].allocatableMemory",
        missing_message=f"第 {index + 1} 台节点缺少可分配内存字段",
    )


def _container_memory(raw: dict[str, Any]) -> int | None:
    containers = _dig(raw, "spec.containers")
    if containers is MISSING:
        containers = raw.get("containers")
    if not isinstance(containers, list):
        return None

    total = 0
    found = False
    for container in containers:
        if not isinstance(container, dict):
            continue
        value = _first(
            container,
            (
                "resources.requests.memory",
                "resources.limits.memory",
                "memoryRequest",
                "memory",
            ),
        )
        if value is not MISSING:
            found = True
            total += parse_memory_bytes(value, field="container memory request")
    return total if found else None


def _volume_zones(raw: dict[str, Any]) -> list[str]:
    value = _first(
        raw,
        (
            "volumeZones",
            "volumeAzs",
            "volumeZone",
            "volumeAz",
            "volumeAvailabilityZone",
            "volume_zone",
            "volume_az",
            "卷可用区",
            "volume.zone",
            "volume.az",
            "volume.availabilityZone",
            "volume.availability_zone",
        ),
    )

    zones: list[str] = []
    if value is not MISSING:
        if isinstance(value, list):
            zones.extend(_string(item) for item in value if _string(item))
        else:
            zones.append(_string(value))

    volumes = _dig(raw, "spec.volumes")
    if volumes is MISSING:
        volumes = raw.get("volumes")
    if isinstance(volumes, list):
        for volume in volumes:
            if not isinstance(volume, dict):
                continue
            zone = _first(volume, ("zone", "az", "availabilityZone", "availability_zone", "卷可用区"))
            if zone is not MISSING and _string(zone):
                zones.append(_string(zone))

    # Deduplicate while preserving order.
    return list(dict.fromkeys(zones))


def normalize_snapshot(payload: Any) -> dict[str, Any]:
    """Validate raw JSON and return the solver's canonical structure."""

    raw_nodes = _extract_node_list(payload)
    if not isinstance(raw_nodes, list):
        raise SnapshotError("nodes 必须是列表")
    if len(raw_nodes) == 0:
        raise SnapshotError("快照至少需要 1 台节点")
    if len(raw_nodes) > 8:
        raise SnapshotError(f"最多支持 8 台节点，当前快照包含 {len(raw_nodes)} 台")

    nodes: list[dict[str, Any]] = []
    seen_node_names: set[str] = set()
    for index, raw_node in enumerate(raw_nodes):
        if not isinstance(raw_node, dict):
            raise SnapshotError(f"nodes[{index}] 必须是 JSON 对象")

        name_value = _first(
            raw_node,
            ("name", "nodeName", "node_name", "id", "hostname", "node", "名称", "节点名称", "metadata.name"),
        )
        name = _string(_require(name_value, f"nodes[{index}] 缺少 name"))
        if name in seen_node_names:
            raise SnapshotError(f"节点名称重复：{name}")
        seen_node_names.add(name)

        memory_bytes = _node_memory(raw_node, index)
        nodes.append(
            {
                "name": name,
                "zone": _node_zone(raw_node),
                "allocatableBytes": memory_bytes,
                "allocatableMemoryMiB": bytes_to_mib(memory_bytes),
            }
        )

    raw_pods = _extract_pod_list(payload, raw_nodes)
    if not isinstance(raw_pods, list):
        raise SnapshotError("pods 必须是列表")

    pods: list[dict[str, Any]] = []
    seen_pod_names: set[str] = set()
    node_names = seen_node_names

    for index, raw_pod in enumerate(raw_pods):
        if not isinstance(raw_pod, dict):
            raise SnapshotError(f"pods[{index}] 必须是 JSON 对象")

        name_value = _first(
            raw_pod,
            ("name", "podName", "pod_name", "id", "名称", "Pod名称", "metadata.name"),
        )
        name = _string(_require(name_value, f"pods[{index}] 缺少 name"))
        if name in seen_pod_names:
            raise SnapshotError(f"Pod 名称重复：{name}")
        seen_pod_names.add(name)

        node_value = _first(
            raw_pod,
            (
                "node",
                "nodeName",
                "node_name",
                "nodeId",
                "host",
                "hostName",
                "location",
                "位置",
                "所在节点",
                "节点",
                "节点名称",
                "spec.nodeName",
            ),
        )
        node_name = _string(_require(node_value, f"Pod {name} 缺少所在节点 node"))
        if node_name not in node_names:
            raise SnapshotError(f"Pod {name} 位于未知节点 {node_name!r}")

        app_value = _first(
            raw_pod,
            (
                "app",
                "application",
                "appName",
                "app_name",
                "应用",
                "应用名称",
                "labels.app",
                "metadata.labels.app",
            ),
        )
        if app_value is MISSING:
            for labels in (raw_pod.get("labels"), _dig(raw_pod, "metadata.labels")):
                if isinstance(labels, dict):
                    for label_key in (
                        "app",
                        "app.kubernetes.io/name",
                        "application",
                        "appName",
                    ):
                        if labels.get(label_key):
                            app_value = labels[label_key]
                            break
                if app_value is not MISSING:
                    break
        app = _string(_require(app_value, f"Pod {name} 缺少应用标识 app"))

        interruptible_value = _first(
            raw_pod,
            (
                "interruptible",
                "interruptable",
                "isInterruptible",
                "isInterruptable",
                "canInterrupt",
                "canBeInterrupted",
                "disruptable",
                "evictable",
                "canEvict",
                "preemptible",
                "spot",
                "canStop",
                "stoppable",
                "能否中断",
                "可中断",
            ),
        )
        # A missing flag is treated conservatively as "must not be stopped".
        interruptible = (
            False
            if interruptible_value is MISSING
            else parse_bool(interruptible_value, field=f"pods[{index}].interruptible")
        )

        type_value = _first(
            raw_pod,
            ("type", "workloadType", "workload_type", "podType", "category", "类型", "labels.type", "labels.workload-type"),
        )
        type_label = _string(type_value) if type_value is not MISSING else ""
        workload_type = classify_workload_type(type_value, interruptible)

        memory_value = _first(
            raw_pod,
            (
                "memoryRequestMiB",
                "memory_request_mib",
                "memoryRequestMB",
                "memoryRequest",
                "memory_request",
                "requestMemoryMiB",
                "requestMemory",
                "memoryMiB",
                "memory",
                "内存请求",
                "resources.requests.memory",
            ),
        )
        has_exact_memory = any(
            raw_pod.get(key) is not None
            for key in ("memoryBytes", "memory_bytes", "__exactMemoryBytes__")
        )
        if memory_value is MISSING and not has_exact_memory:
            container_memory = _container_memory(raw_pod)
            if container_memory is None:
                raise SnapshotError(f"Pod {name} 缺少内存请求 memoryRequestMiB")
            memory_bytes = container_memory
        else:
            memory_bytes = _resolve_memory_bytes(
                raw_pod,
                exact_keys=("memoryBytes", "memory_bytes"),
                human_keys=(
                    "memoryRequestMiB",
                    "memory_request_mib",
                    "memoryRequestMB",
                    "memoryRequest",
                    "memory_request",
                    "requestMemoryMiB",
                    "requestMemory",
                    "memoryMiB",
                    "memory",
                    "内存请求",
                    "resources.requests.memory",
                ),
                index_label=f"pods[{index}].memoryRequest",
                missing_message=f"Pod {name} 缺少内存请求 memoryRequestMiB",
            )

        volume_zones = _volume_zones(raw_pod)
        eligible_to_stop = workload_type == "batch" and interruptible

        pods.append(
            {
                "name": name,
                "node": node_name,
                "app": app,
                "typeLabel": type_label or workload_type,
                "workloadType": workload_type,
                "memoryBytes": memory_bytes,
                "memoryRequestMiB": bytes_to_mib(memory_bytes),
                "volumeZones": volume_zones,
                "volumeZone": volume_zones[0] if volume_zones else None,
                "interruptible": interruptible,
                "eligibleToStop": eligible_to_stop,
            }
        )

    return {"nodes": nodes, "pods": pods}
