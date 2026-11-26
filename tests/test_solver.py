from __future__ import annotations

import json
import unittest
from pathlib import Path

from app.memory import parse_memory_bytes
from app.normalize import SnapshotError, normalize_snapshot
from app.solver import RecoveryError, plan_recovery, recover_from_payload

ROOT = Path(__file__).resolve().parents[1]


def node(name: str, zone: str = "az-a", memory: int = 10240) -> dict:
    return {"name": name, "zone": zone, "allocatableMemoryMiB": memory}


def pod(
    name: str,
    node_name: str,
    app: str | None = None,
    *,
    pod_type: str = "online",
    memory: int = 1024,
    zone: str | None = None,
    interruptible: bool = False,
) -> dict:
    data = {
        "name": name,
        "node": node_name,
        "app": app or name.rsplit("-", 1)[0],
        "type": pod_type,
        "memoryRequestMiB": memory,
        "interruptible": interruptible,
    }
    if zone is not None:
        data["volumeZone"] = zone
    return data


class SolverTests(unittest.TestCase):
    def test_repository_example_is_feasible_with_one_stop(self):
        payload = json.loads((ROOT / "examples" / "snapshot.json").read_text(encoding="utf-8"))
        result = recover_from_payload(payload, "k8s-node-1")

        self.assertTrue(result["feasible"], result.get("blockers"))
        self.assertEqual(4, result["sourceOnlineCount"])
        self.assertEqual(4, len(result["placements"]))
        self.assertEqual(1, result["minimumStoppedPods"])
        self.assertEqual(["batch-reindex"], [item["pod"] for item in result["stops"]])

        target_by_pod = {item["pod"]: item for item in result["placements"]}
        self.assertEqual("k8s-node-4", target_by_pod["order-7"]["node"])
        self.assertEqual("k8s-node-3", target_by_pod["payment-7"]["node"])
        self.assertEqual("az-b", target_by_pod["payment-7"]["zone"])

    def test_every_placement_satisfies_runtime_constraints(self):
        payload = json.loads((ROOT / "examples" / "snapshot.json").read_text(encoding="utf-8"))
        snapshot = normalize_snapshot(payload)
        result = plan_recovery(snapshot, "k8s-node-1")
        self.assertTrue(result["feasible"])

        nodes = {item["name"]: item for item in snapshot["nodes"]}
        pods = snapshot["pods"]
        stopped = {item["pod"] for item in result["stops"]}
        placements = result["placements"]

        # Volume zones.
        for placement in placements:
            if placement["volumeZone"]:
                self.assertEqual(placement["volumeZone"], nodes[placement["node"]]["zone"])

        # Final anti-affinity, including retained pods and stopped removals.
        final_by_node: dict[str, list[dict]] = {}
        for raw in pods:
            if raw["node"] == "k8s-node-1" or raw["name"] in stopped:
                continue
            final_by_node.setdefault(raw["node"], []).append(raw)
        for placement in placements:
            final_by_node.setdefault(placement["node"], []).append(
                {"app": placement["app"], "name": placement["pod"]}
            )
        for node_name, final_pods in final_by_node.items():
            apps = [item["app"] for item in final_pods]
            self.assertEqual(len(apps), len(set(apps)), node_name)

        # Final memory.
        for report in result["nodeReports"]:
            node = nodes[report["node"]]
            self.assertLessEqual(report["afterUsedMiB"], node["allocatableMemoryMiB"])

    def test_volume_zone_without_matching_node_is_unrecoverable(self):
        payload = {
            "nodes": [node("failed", "az-a"), node("healthy", "az-a")],
            "pods": [pod("db-0", "failed", "db", memory=512, zone="az-b")],
        }
        result = recover_from_payload(payload, "failed")
        self.assertFalse(result["feasible"])
        self.assertIn("无法恢复", result["message"])
        self.assertTrue(result["blockers"])

    def test_online_same_app_replica_blocks_destination(self):
        payload = {
            "nodes": [node("failed"), node("healthy", memory=10240)],
            "pods": [
                pod("web-0", "failed", "web", memory=1024),
                pod("web-1", "healthy", "web", memory=1024),
            ],
        }
        result = recover_from_payload(payload, "failed")
        self.assertFalse(result["feasible"])

    def test_noninterruptible_batch_cannot_be_stopped_for_memory(self):
        payload = {
            "nodes": [node("failed"), node("healthy", memory=2048)],
            "pods": [
                pod("api-0", "failed", "api", memory=2048),
                pod("batch-hard", "healthy", "hardjob", pod_type="batch", memory=2048, interruptible=False),
            ],
        }
        result = recover_from_payload(payload, "failed")
        self.assertFalse(result["feasible"])

    def test_interruptible_batch_can_free_memory_with_minimum_count(self):
        payload = {
            "nodes": [node("failed"), node("healthy", memory=10240)],
            "pods": [
                pod("api-0", "failed", "api", memory=5120),
                pod("batch-big", "healthy", "big", pod_type="batch", memory=6144, interruptible=True),
                pod("batch-small", "healthy", "small", pod_type="batch", memory=3072, interruptible=True),
            ],
        }
        # Existing request is 9216/10240. Adding 5120 creates a 4096 MiB
        # deficit; the 6144 MiB pod alone is the unique one-stop solution.
        result = recover_from_payload(payload, "failed")
        self.assertTrue(result["feasible"], result.get("blockers"))
        self.assertEqual(1, result["minimumStoppedPods"])
        self.assertEqual(["batch-big"], [item["pod"] for item in result["stops"]])

    def test_two_replicas_of_same_app_need_two_nodes(self):
        payload = {
            "nodes": [node("failed"), node("only-healthy")],
            "pods": [
                pod("web-0", "failed", "web", memory=512),
                pod("web-1", "failed", "web", memory=512),
            ],
        }
        result = recover_from_payload(payload, "failed")
        self.assertFalse(result["feasible"])

        payload["nodes"].append(node("second-healthy"))
        result = recover_from_payload(payload, "failed")
        self.assertTrue(result["feasible"], result.get("blockers"))
        targets = {item["node"] for item in result["placements"]}
        self.assertEqual({"only-healthy", "second-healthy"}, targets)

    def test_failed_node_without_online_pods_needs_no_action(self):
        payload = {
            "nodes": [node("failed"), node("healthy")],
            "pods": [
                pod("job-0", "failed", "job", pod_type="batch", interruptible=True),
            ],
        }
        result = recover_from_payload(payload, "failed")
        self.assertTrue(result["feasible"])
        self.assertEqual(0, result["sourceOnlineCount"])
        self.assertEqual(0, result["minimumStoppedPods"])

    def test_missing_failed_node_is_request_error(self):
        snapshot = normalize_snapshot({"nodes": [node("a")], "pods": []})
        with self.assertRaises(RecoveryError):
            plan_recovery(snapshot, "missing")

    def test_more_than_eight_nodes_is_rejected(self):
        payload = {"nodes": [node(f"n{i}") for i in range(9)], "pods": []}
        with self.assertRaises(SnapshotError):
            normalize_snapshot(payload)

    def test_more_than_eight_online_pods_is_rejected(self):
        payload = {
            "nodes": [node("failed"), node("healthy")],
            "pods": [pod(f"app{i}-0", "failed", f"app{i}") for i in range(9)],
        }
        snapshot = normalize_snapshot(payload)
        with self.assertRaises(RecoveryError):
            plan_recovery(snapshot, "failed")

    def test_chinese_field_aliases_are_supported(self):
        payload = {
            "节点": [{"名称": "n1", "可用区": "甲", "可分配内存": 2048}],
            "pods": [
                {
                    "名称": "p1",
                    "位置": "n1",
                    "应用": "a",
                    "类型": "在线",
                    "内存请求": 1024,
                    "卷可用区": "甲",
                    "能否中断": "否",
                }
            ],
        }
        # Top-level Chinese node key is not an advertised alias; the documented
        # shape uses nodes. This test mainly verifies Chinese field aliases.
        payload = {
            "nodes": [{"名称": "n1", "可用区": "甲", "可分配内存": 2048}],
            "pods": payload["pods"],
        }
        snapshot = normalize_snapshot(payload)
        self.assertEqual("甲", snapshot["nodes"][0]["zone"])
        self.assertFalse(snapshot["pods"][0]["interruptible"])

    def test_memory_quantity_parser(self):
        self.assertEqual(2048 * 1024**2, parse_memory_bytes(2048))
        self.assertEqual(2 * 1024**3, parse_memory_bytes("2Gi"))
        self.assertEqual(512 * 1024**2, parse_memory_bytes("512Mi"))
        self.assertEqual(2_000_000_000, parse_memory_bytes("2G"))


class FlaskRouteTests(unittest.TestCase):
    def setUp(self):
        try:
            import flask  # noqa: F401
        except ImportError:
            self.skipTest("Flask is not installed in this environment")
        from app import create_app

        self.client = create_app().test_client()

    def test_index_and_health(self):
        self.assertEqual(200, self.client.get("/").status_code)
        health = self.client.get("/healthz")
        self.assertEqual(200, health.status_code)
        self.assertEqual("ok", health.get_json()["status"])

    def test_api_recover(self):
        payload = json.loads((ROOT / "examples" / "snapshot.json").read_text(encoding="utf-8"))
        response = self.client.post("/api/recover", json={"snapshot": payload, "failed_node": "k8s-node-1"})
        self.assertEqual(200, response.status_code)
        body = response.get_json()
        self.assertTrue(body["feasible"])
        self.assertEqual(1, body["minimumStoppedPods"])

    def test_form_flow_renders_result(self):
        payload = json.loads((ROOT / "examples" / "snapshot.json").read_text(encoding="utf-8"))
        response = self.client.post(
            "/plan",
            data={"snapshot": json.dumps(payload), "failed_node": "k8s-node-1"},
        )
        self.assertEqual(200, response.status_code)
        self.assertIn("全部迁移落点", response.text)
        self.assertIn("batch-reindex", response.text)


if __name__ == "__main__":
    unittest.main()
