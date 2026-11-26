"""Exact recovery planner.

The planner enumerates every possible subset of failed-node online pods that
could land on one healthy node (<= 8 source pods).  For each subset it finds
the minimum number of eligible batch pods to stop in order to satisfy memory
and application anti-affinity.  A bit-mask DP then combines node-local options
and minimizes the global number of stopped pods.
"""

from __future__ import annotations

from typing import Any

from .memory import bytes_to_mib
from .normalize import SnapshotError, normalize_snapshot


class RecoveryError(ValueError):
    """Raised when the recovery request itself is invalid."""


class _BundleInfeasible(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def recover_from_payload(payload: Any, failed_node: Any) -> dict[str, Any]:
    snapshot = normalize_snapshot(payload)
    return plan_recovery(snapshot, failed_node)


def _memory_fits(used_bytes: int, allocatable_bytes: int) -> bool:
    return used_bytes <= allocatable_bytes


def _evaluate_bundle(
    *,
    node: dict[str, Any],
    mask: int,
    source_pods: list[dict[str, Any]],
    local_pods: list[dict[str, Any]],
    pods_by_name_on_node: dict[str, dict[str, Any]],
    eligible_candidates_sorted: list[dict[str, Any]],
    groups_by_app: dict[str, list[dict[str, Any]]],
    used_bytes: int,
) -> dict[str, Any]:
    bundle = [source_pods[index] for index in range(len(source_pods)) if mask & (1 << index)]
    bundle_apps = [pod["app"] for pod in bundle]

    if len(set(bundle_apps)) != len(bundle_apps):
        duplicated = sorted({app for app in bundle_apps if bundle_apps.count(app) > 1})
        raise _BundleInfeasible(f"同一节点不能接收同应用的多个副本：{', '.join(duplicated)}")

    node_zone = node.get("zone")
    for pod in bundle:
        volume_zones = pod.get("volumeZones") or []
        for volume_zone in volume_zones:
            if node_zone != volume_zone:
                raise _BundleInfeasible(
                    f"卷可用区为 {volume_zone}，但节点可用区为 {node_zone or '未设置'}"
                )

    required_stops: dict[str, str] = {}
    bundle_app_set = set(bundle_apps)

    # If an incoming pod belongs to app A, no retained A pod may remain.
    for app in bundle_app_set:
        for existing in groups_by_app.get(app, []):
            if not existing["eligibleToStop"]:
                kind = "可中断批作业" if existing["workloadType"] == "batch" else "在线/不可停用"
                raise _BundleInfeasible(
                    f"同应用 Pod {existing['name']}（{kind}）正在节点上运行且不能停用"
                )
            required_stops[existing["name"]] = "anti_affinity"

    # Keep the final node valid even when the snapshot already contains
    # multiple same-app pods. At most one existing replica may survive.
    for app, group in groups_by_app.items():
        if app in bundle_app_set or len(group) <= 1:
            continue

        non_eligible = [pod for pod in group if not pod["eligibleToStop"]]
        if len(non_eligible) > 1:
            names = ", ".join(pod["name"] for pod in non_eligible)
            raise _BundleInfeasible(f"应用 {app} 已有多个不可停用的同节点副本：{names}")

        if len(non_eligible) == 1:
            survivor = non_eligible[0]
            for pod in group:
                if pod["name"] != survivor["name"]:
                    # Eligibility is guaranteed because there is only one
                    # non-eligible pod in this group.
                    required_stops[pod["name"]] = "anti_affinity_existing"
        else:
            # All replicas are eligible. Requiring the largest c-1 memory
            # pods to stop maximizes freed memory for this mandatory count.
            survivor = sorted(group, key=lambda pod: (pod["memoryBytes"], pod["name"]))[0]
            for pod in group:
                if pod["name"] != survivor["name"]:
                    required_stops[pod["name"]] = "anti_affinity_existing"

    required_bytes = sum(
        pods_by_name_on_node[name]["memoryBytes"] for name in required_stops
    )
    bundle_bytes = sum(pod["memoryBytes"] for pod in bundle)

    # Amount that must be freed from the current state to fit the bundle.
    total_used_with_bundle = used_bytes + bundle_bytes
    memory_deficit = max(0, total_used_with_bundle - node["allocatableBytes"])
    additional_need = max(0, memory_deficit - required_bytes)

    optional_candidates = [
        pod for pod in eligible_candidates_sorted if pod["name"] not in required_stops
    ]

    stop_reasons = dict(required_stops)
    additional_freed = 0
    for pod in optional_candidates:
        if additional_freed >= additional_need:
            break
        stop_reasons[pod["name"]] = "memory"
        additional_freed += pod["memoryBytes"]

    if additional_freed < additional_need:
        max_releasable = required_bytes + sum(pod["memoryBytes"] for pod in optional_candidates)
        raise _BundleInfeasible(
            "即使停用该节点上全部可中断批作业，内存仍不足："
            f"需要释放约 {bytes_to_mib(memory_deficit):g} MiB，"
            f"最多可释放 {bytes_to_mib(max_releasable):g} MiB"
        )

    all_stop_bytes = sum(pods_by_name_on_node[name]["memoryBytes"] for name in stop_reasons)
    final_used_bytes = used_bytes + bundle_bytes - all_stop_bytes
    if final_used_bytes > node["allocatableBytes"]:  # defensive invariant
        raise _BundleInfeasible("内存试算结果超过节点可分配内存")

    return {
        "mask": mask,
        "placedNames": tuple(pod["name"] for pod in bundle),
        "stopNames": tuple(sorted(stop_reasons)),
        "stopReasons": tuple(sorted(stop_reasons.items())),
        "stopCount": len(stop_reasons),
        "bundleBytes": bundle_bytes,
        "stopBytes": all_stop_bytes,
        "finalUsedBytes": final_used_bytes,
    }


def _diagnose(
    *,
    healthy_nodes: list[dict[str, Any]],
    source_pods: list[dict[str, Any]],
    singleton_errors: dict[str, dict[int, str]],
    node_zero_errors: list[str] | None = None,
    no_healthy_reason: str | None = None,
) -> list[str]:
    blockers: list[str] = []
    if no_healthy_reason:
        return [no_healthy_reason]

    blockers.extend(node_zero_errors or [])

    for index, pod in enumerate(source_pods):
        impossible_reasons = []
        for node in healthy_nodes:
            reason = singleton_errors.get(node["name"], {}).get(index)
            if reason:
                impossible_reasons.append(f"{node['name']}（{reason}）")
        if len(impossible_reasons) == len(healthy_nodes):
            shown = "；".join(impossible_reasons[:4])
            if len(impossible_reasons) > 4:
                shown += "；…"
            blockers.append(
                f"Pod {pod['name']} / 应用 {pod['app']} 在所有健康节点上均无单独落点：{shown}"
            )

    if not blockers:
        blockers.append(
            "每个 Pod 可能有单独落点，但多个 Pod 对可用区、反亲和节点或可释放内存的组合需求冲突，"
            "不存在覆盖全部在线 Pod 的完整方案。"
        )
    return blockers


def plan_recovery(snapshot: dict[str, Any], failed_node: Any) -> dict[str, Any]:
    if not isinstance(snapshot, dict) or "nodes" not in snapshot or "pods" not in snapshot:
        raise RecoveryError("请先提供并校验快照")

    failed_name = str(failed_node or "").strip()
    if not failed_name:
        raise RecoveryError("请选择一台故障节点")

    nodes = snapshot["nodes"]
    pods = snapshot["pods"]
    node_by_name = {node["name"]: node for node in nodes}
    if failed_name not in node_by_name:
        raise RecoveryError(f"故障节点 {failed_name!r} 不在快照中")

    failed_node_obj = node_by_name[failed_name]
    source_pods = [
        pod
        for pod in pods
        if pod["node"] == failed_name and pod["workloadType"] == "online"
    ]
    if len(source_pods) > 8:
        raise RecoveryError(
            f"故障节点上有 {len(source_pods)} 个在线 Pod；本演练最多支持 8 个，请缩小快照范围"
        )

    healthy_nodes = [node for node in nodes if node["name"] != failed_name]
    if not healthy_nodes:
        result = _infeasible_result(
            failed_name=failed_name,
            failed_node_obj=failed_node_obj,
            source_pods=source_pods,
            blockers=["快照中没有除故障节点之外的健康节点，无法迁入任何在线 Pod。"],
        )
        return result

    pods_by_name = {pod["name"]: pod for pod in pods}
    pods_by_node: dict[str, list[dict[str, Any]]] = {node["name"]: [] for node in nodes}
    for pod in pods:
        pods_by_node.setdefault(pod["node"], []).append(pod)

    source_count = len(source_pods)
    full_mask = (1 << source_count) - 1

    # dp[covered_mask] = best state after processing a prefix of nodes.
    dp: dict[int, dict[str, Any]] = {
        0: {
            "stopCount": 0,
            "stopNames": (),
            "stopReasons": (),
            "placements": (),
            "rank": (0, (), ()),
        }
    }

    singleton_errors: dict[str, dict[int, str]] = {}
    node_zero_errors: list[str] = []

    for node in healthy_nodes:
        local_pods = pods_by_node.get(node["name"], [])
        local_by_name = {pod["name"]: pod for pod in local_pods}
        eligible_by_name = {
            pod["name"]: pod for pod in local_pods if pod["eligibleToStop"]
        }
        eligible_candidates_sorted = sorted(
            eligible_by_name.values(),
            key=lambda pod: (-pod["memoryBytes"], pod["name"]),
        )
        groups_by_app: dict[str, list[dict[str, Any]]] = {}
        for pod in local_pods:
            groups_by_app.setdefault(pod["app"], []).append(pod)
        used_bytes = sum(pod["memoryBytes"] for pod in local_pods)

        options: dict[int, dict[str, Any]] = {}
        node_errors: dict[int, str] = {}
        for mask in range(1 << source_count):
            try:
                options[mask] = _evaluate_bundle(
                    node=node,
                    mask=mask,
                    source_pods=source_pods,
                    local_pods=local_pods,
                    pods_by_name_on_node=local_by_name,
                    eligible_candidates_sorted=eligible_candidates_sorted,
                    groups_by_app=groups_by_app,
                    used_bytes=used_bytes,
                )
            except _BundleInfeasible as exc:
                node_errors[mask] = exc.reason
                if mask == 0:
                    node_zero_errors.append(f"节点 {node['name']}：{exc.reason}")

        for index in range(source_count):
            singleton_mask = 1 << index
            if singleton_mask in node_errors:
                singleton_errors.setdefault(node["name"], {})[index] = node_errors[singleton_mask]

        next_dp: dict[int, dict[str, Any]] = {}
        for covered, state in dp.items():
            for mask, option in options.items():
                if covered & mask:
                    continue
                new_covered = covered | mask
                stop_names = tuple(sorted(set(state["stopNames"]) | set(option["stopNames"])))
                stop_reasons = tuple(
                    sorted(dict(state["stopReasons"] + option["stopReasons"]).items())
                )
                placements = tuple(
                    sorted(
                        set(state["placements"])
                        | {(name, node["name"]) for name in option["placedNames"]}
                    )
                )
                stop_count = state["stopCount"] + option["stopCount"]
                rank = (stop_count, stop_names, placements)
                candidate = {
                    "stopCount": stop_count,
                    "stopNames": stop_names,
                    "stopReasons": stop_reasons,
                    "placements": placements,
                    "rank": rank,
                }
                existing = next_dp.get(new_covered)
                if existing is None or rank < existing["rank"]:
                    next_dp[new_covered] = candidate

        dp = next_dp
        if not dp:
            # No state can remain valid after this node; later nodes cannot
            # repair a permanent local anti-affinity violation.
            break

    if full_mask not in dp:
        blockers = _diagnose(
            healthy_nodes=healthy_nodes,
            source_pods=source_pods,
            singleton_errors=singleton_errors,
            node_zero_errors=node_zero_errors,
        )
        return _infeasible_result(
            failed_name=failed_name,
            failed_node_obj=failed_node_obj,
            source_pods=source_pods,
            blockers=blockers,
        )

    state = dp[full_mask]
    target_by_pod_name = dict(state["placements"])
    reason_by_stop = dict(state["stopReasons"])
    node_order = {node["name"]: index for index, node in enumerate(nodes)}

    placements: list[dict[str, Any]] = []
    for pod in source_pods:
        target_name = target_by_pod_name.get(pod["name"])
        if target_name is None:  # defensive; full_mask guarantees coverage
            raise RecoveryError("求解器未给全部在线 Pod 分配落点")
        target_node = node_by_name[target_name]
        placements.append(
            {
                "pod": pod["name"],
                "app": pod["app"],
                "type": pod["workloadType"],
                "typeLabel": pod["typeLabel"],
                "memoryRequestMiB": pod["memoryRequestMiB"],
                "memoryRequestBytes": pod["memoryBytes"],
                "volumeZones": pod["volumeZones"],
                "volumeZone": pod.get("volumeZone"),
                "node": target_name,
                "zone": target_node.get("zone"),
            }
        )

    stops: list[dict[str, Any]] = []
    for stop_name in state["stopNames"]:
        pod = pods_by_name[stop_name]
        reason_code = reason_by_stop.get(stop_name, "memory")
        stops.append(
            {
                "pod": stop_name,
                "node": pod["node"],
                "app": pod["app"],
                "type": pod["workloadType"],
                "memoryRequestMiB": pod["memoryRequestMiB"],
                "memoryRequestBytes": pod["memoryBytes"],
                "interruptible": True,
                "reasonCode": reason_code,
                "reason": (
                    "消除同应用副本冲突"
                    if reason_code.startswith("anti_affinity")
                    else "停用可中断批作业以释放内存"
                ),
            }
        )
    stops.sort(key=lambda item: (node_order.get(item["node"], 999), item["pod"]))

    node_reports = _build_node_reports(
        healthy_nodes=healthy_nodes,
        pods_by_node=pods_by_node,
        placements=placements,
        stops=stops,
    )

    if source_count == 0 and not stops:
        message = f"节点 {failed_name} 上没有需要迁移的在线 Pod，无需停用其他 Pod。"
    else:
        message = (
            f"已找到完整恢复方案：{source_count} 个在线 Pod 全部有落点，"
            f"最少停用 {len(stops)} 个可中断批作业 Pod。"
        )

    return {
        "ok": True,
        "feasible": True,
        "failedNode": failed_name,
        "failedNodeZone": failed_node_obj.get("zone"),
        "sourceOnlinePods": [pod["name"] for pod in source_pods],
        "sourceOnlineCount": source_count,
        "minimumStoppedPods": len(stops),
        "placements": placements,
        "stops": stops,
        "nodeReports": node_reports,
        "message": message,
    }


def _infeasible_result(
    *,
    failed_name: str,
    failed_node_obj: dict[str, Any],
    source_pods: list[dict[str, Any]],
    blockers: list[str],
) -> dict[str, Any]:
    return {
        "ok": False,
        "feasible": False,
        "failedNode": failed_name,
        "failedNodeZone": failed_node_obj.get("zone"),
        "sourceOnlinePods": [pod["name"] for pod in source_pods],
        "sourceOnlineCount": len(source_pods),
        "minimumStoppedPods": None,
        "placements": [],
        "stops": [],
        "nodeReports": [],
        "blockers": blockers,
        "message": "无法恢复：不存在满足全部约束的完整迁移方案，未采用部分迁移结果。",
    }


def _build_node_reports(
    *,
    healthy_nodes: list[dict[str, Any]],
    pods_by_node: dict[str, list[dict[str, Any]]],
    placements: list[dict[str, Any]],
    stops: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    placed_by_node: dict[str, list[dict[str, Any]]] = {}
    for placement in placements:
        placed_by_node.setdefault(placement["node"], []).append(placement)

    stopped_by_node: dict[str, list[dict[str, Any]]] = {}
    for stop in stops:
        stopped_by_node.setdefault(stop["node"], []).append(stop)

    reports = []
    for node in healthy_nodes:
        local_pods = pods_by_node.get(node["name"], [])
        stopped_here = stopped_by_node.get(node["name"], [])
        stopped_name_set = {item["pod"] for item in stopped_here}
        retained_pods = [pod["name"] for pod in local_pods if pod["name"] not in stopped_name_set]
        placed_here = placed_by_node.get(node["name"], [])
        before_bytes = sum(pod["memoryBytes"] for pod in local_pods)
        stop_bytes = sum(item["memoryRequestBytes"] for item in stopped_here)
        placed_bytes = sum(item["memoryRequestBytes"] for item in placed_here)
        after_bytes = before_bytes - stop_bytes + placed_bytes
        allocatable = node["allocatableBytes"]
        placed_names = [item["pod"] for item in placed_here]
        stopped_names = [item["pod"] for item in stopped_here]
        reports.append(
            {
                "node": node["name"],
                "zone": node.get("zone"),
                "allocatableMemoryMiB": node["allocatableMemoryMiB"],
                "beforeUsedMiB": bytes_to_mib(before_bytes),
                "stoppedMemoryMiB": bytes_to_mib(stop_bytes),
                "placedMemoryMiB": bytes_to_mib(placed_bytes),
                "afterUsedMiB": bytes_to_mib(after_bytes),
                "availableAfterMiB": bytes_to_mib(max(0, allocatable - after_bytes)),
                "utilizationPercent": round(100.0 * after_bytes / allocatable, 1) if allocatable else None,
                "placedPods": placed_names,
                "stoppedPods": stopped_names,
                "retainedPods": retained_pods,
                "finalPods": retained_pods + placed_names,
                "retainedPodCount": len(retained_pods),
            }
        )
    return reports
