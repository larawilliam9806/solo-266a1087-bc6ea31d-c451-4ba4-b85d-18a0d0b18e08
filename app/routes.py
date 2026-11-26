"""HTTP routes for the recovery workbench and JSON API."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from flask import (
    Blueprint,
    current_app,
    jsonify,
    render_template,
    request,
    send_from_directory,
)
from werkzeug.exceptions import RequestEntityTooLarge

from .normalize import SnapshotError, normalize_snapshot
from .solver import RecoveryError, plan_recovery

bp = Blueprint("views", __name__)


def _load_json_text(raw_text: str | None) -> Any:
    if not raw_text or not raw_text.strip():
        raise SnapshotError("请上传 JSON 文件，或在文本框中粘贴 JSON 快照")
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise SnapshotError(
            f"JSON 解析失败：第 {exc.lineno} 行第 {exc.colno} 列附近，{exc.msg}"
        ) from exc


def _read_snapshot_from_form() -> tuple[Any, str | None]:
    upload = request.files.get("snapshot_file")
    if upload is not None and upload.filename:
        raw_bytes = upload.read()
        try:
            raw_text = raw_bytes.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise SnapshotError("JSON 文件必须使用 UTF-8 编码") from exc
        return _load_json_text(raw_text), upload.filename

    raw_text = request.form.get("snapshot_text")
    if raw_text is None:
        raw_text = request.form.get("snapshot")
    return _load_json_text(raw_text), None


def _render_workbench_from_payload(payload: Any, *, error: str | None = None, status: int = 200):
    snapshot = normalize_snapshot(payload)
    snapshot_json = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
    pretty_json = json.dumps(snapshot, ensure_ascii=False, indent=2)
    return (
        render_template(
            "workbench.html",
            snapshot=snapshot,
            snapshot_json=snapshot_json,
            pretty_json=pretty_json,
            error=error,
        ),
        status,
    )


@bp.get("/")
def index():
    return render_template("index.html", error=None)


@bp.post("/snapshot")
@bp.post("/upload")
def snapshot_upload():
    try:
        payload, _filename = _read_snapshot_from_form()
        return _render_workbench_from_payload(payload)
    except SnapshotError as exc:
        return render_template("index.html", error=str(exc)), 400


@bp.post("/workbench")
def workbench_restore():
    try:
        payload = _load_json_text(request.form.get("snapshot"))
        return _render_workbench_from_payload(payload)
    except SnapshotError as exc:
        return render_template("index.html", error=str(exc)), 400


@bp.post("/plan")
def plan_form():
    try:
        payload = _load_json_text(request.form.get("snapshot"))
        snapshot = normalize_snapshot(payload)
        failed_node = request.form.get("failed_node") or request.form.get("failedNode")
        if not failed_node:
            return _render_workbench_from_payload(
                snapshot, error="请选择一台故障节点", status=400
            )
        result = plan_recovery(snapshot, failed_node)
        return render_template("result.html", snapshot=snapshot, result=result)
    except (SnapshotError, RecoveryError) as exc:
        # If normalization failed before a snapshot existed, go back to upload.
        try:
            payload = _load_json_text(request.form.get("snapshot"))
            return _render_workbench_from_payload(payload, error=str(exc), status=400)
        except SnapshotError:
            return render_template("index.html", error=str(exc)), 400


@bp.post("/api/recover")
@bp.post("/api/plan")
def api_recover():
    data = request.get_json(silent=True)
    if data is None:
        raw_body = request.get_data(as_text=True)
        try:
            data = json.loads(raw_body) if raw_body.strip() else None
        except json.JSONDecodeError as exc:
            return (
                jsonify(
                    {
                        "ok": False,
                        "success": False,
                        "feasible": False,
                        "canRecover": False,
                        "error": f"请求体不是有效 JSON：{exc.msg}",
                    }
                ),
                400,
            )

    if not isinstance(data, dict):
        return (
            jsonify(
                {
                    "ok": False,
                    "success": False,
                    "feasible": False,
                    "canRecover": False,
                    "error": "请求体必须是 JSON 对象",
                }
            ),
            400,
        )

    failed_node = (
        data.get("failed_node")
        or data.get("failedNode")
        or data.get("failed_node_name")
        or data.get("node")
    )
    payload: Any = data.get("snapshot", data)
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            return (
                jsonify(
                    {
                        "ok": False,
                        "success": False,
                        "feasible": False,
                        "canRecover": False,
                        "error": f"snapshot 不是有效 JSON：{exc.msg}",
                    }
                ),
                400,
            )

    try:
        snapshot = normalize_snapshot(payload)
        result = plan_recovery(snapshot, failed_node)
        # Convenience aliases for API consumers / automated checks.
        result["success"] = result["feasible"]
        result["canRecover"] = result["feasible"]
        result["recoverable"] = result["feasible"]
        if result["feasible"]:
            result["assignments"] = result["placements"]
            result["targets"] = result["placements"]
            result["migrations"] = result["placements"]
            result["evictions"] = result["stops"]
            result["stoppedPods"] = result["stops"]
            result["podsToStop"] = result["stops"]
            result["stopped_pods"] = result["stops"]
        return jsonify(result)
    except (SnapshotError, RecoveryError) as exc:
        return (
            jsonify(
                {
                    "ok": False,
                    "success": False,
                    "feasible": False,
                    "canRecover": False,
                    "error": str(exc),
                }
            ),
            400,
        )


@bp.post("/recover")
def recover_any():
    """Convenience alias accepting either JSON or the HTML form encoding."""

    if request.is_json:
        return api_recover()
    return plan_form()


@bp.get("/examples/<path:filename>")
def examples(filename: str):
    examples_dir = Path(current_app.root_path).parent / "examples"
    return send_from_directory(examples_dir, filename)


@bp.get("/healthz")
def healthz():
    return jsonify({"status": "ok"})


@bp.get("/favicon.ico")
def favicon():
    return "", 204


@bp.app_errorhandler(RequestEntityTooLarge)
def too_large(_error: RequestEntityTooLarge):
    message = "快照过大：请控制在 5 MiB 以内，并确保最多 8 台节点"
    if request.path.startswith("/api/"):
        return (
            jsonify({"ok": False, "success": False, "feasible": False, "error": message}),
            413,
        )
    return render_template("index.html", error=message), 413
