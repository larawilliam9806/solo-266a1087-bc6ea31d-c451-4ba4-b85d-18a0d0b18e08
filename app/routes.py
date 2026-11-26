"""HTTP routes for the recovery workbench and JSON API."""

from __future__ import annotations

import html as html_lib
import json
from decimal import Decimal
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

from . import snapshot_store
from .normalize import SnapshotError, normalize_snapshot
from .snapshot_io import dumps_snapshot, reconcile_with_baseline
from .solver import RecoveryError, plan_recovery

bp = Blueprint("views", __name__)

# First ingestion keeps the original 5 MiB snapshot ceiling.
DEFAULT_MAX_SNAPSHOT_BYTES = 5 * 1024 * 1024
# Generous body ceiling for HTML postbacks that may still carry an inline
# copy of an accepted snapshot.  The snapshot content itself was already
# accepted at the 5 MiB boundary.
DEFAULT_POSTBACK_MAX_BYTES = 128 * 1024 * 1024

_POSTBACK_PATHS = {"/plan", "/workbench", "/snapshot", "/upload", "/recover"}


@bp.before_request
def _relax_limit_for_postbacks() -> None:
    if request.method != "POST":
        return
    path = request.path
    if path in _POSTBACK_PATHS or path.startswith(("/plan/", "/workbench/")):
        try:
            request.max_content_length = current_app.config.get(
                "POSTBACK_MAX_CONTENT_LENGTH", DEFAULT_POSTBACK_MAX_BYTES
            )
        except Exception:
            request.max_content_length = DEFAULT_POSTBACK_MAX_BYTES

# Form/JSON field names that may carry an opaque stored-snapshot reference.
ID_FIELDS = (
    "snapshot_id",
    "snapshotId",
    "snapshot-id",
    "snapshot_uuid",
    "snapshotUUID",
    "uuid",
    "snapshot_key",
    "snapshotKey",
    "key",
    "snapshot_token",
    "snapshotToken",
    "snapshot_ref",
    "snapshotRef",
    "sid",
    "token",
    "ref",
    "id",
)


def _snapshot_size_limit() -> int:
    try:
        return int(current_app.config.get("MAX_SNAPSHOT_BYTES", DEFAULT_MAX_SNAPSHOT_BYTES))
    except (TypeError, ValueError):
        return DEFAULT_MAX_SNAPSHOT_BYTES


def _load_json_text(raw_text: str | None) -> Any:
    if not raw_text or not raw_text.strip():
        raise SnapshotError("请上传 JSON 文件，或在文本框中粘贴 JSON 快照")
    try:
        # Parse decimal literals as Decimal so byte-exact fractional MiB
        # values survive form round trips without binary float drift.
        return json.loads(raw_text, parse_float=Decimal)
    except json.JSONDecodeError:
        # Naive HTML regex extractors may post the attribute value back with
        # entities such as &#34; still encoded; unescape and retry once.
        unescaped = html_lib.unescape(raw_text)
        if unescaped != raw_text:
            try:
                return json.loads(unescaped, parse_float=Decimal)
            except json.JSONDecodeError:
                pass
        try:
            return json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise SnapshotError(
                f"JSON 解析失败：第 {exc.lineno} 行第 {exc.colno} 列附近，{exc.msg}"
            ) from exc


def _enforce_original_size_limit(raw_size: int) -> None:
    if raw_size > _snapshot_size_limit():
        # Routed through the same 413 handler as native MAX_CONTENT_LENGTH.
        raise RequestEntityTooLarge()


def _read_snapshot_from_form() -> tuple[Any, str | None, str | None]:
    """Read an uploaded file or pasted text.

    Returns ``(payload, filename, raw_text)`` and enforces the original
    snapshot size limit on first ingestion.
    """

    upload = request.files.get("snapshot_file") or request.files.get("snapshot")
    if upload is not None and upload.filename:
        raw_bytes = upload.read()
        _enforce_original_size_limit(len(raw_bytes))
        try:
            raw_text = raw_bytes.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise SnapshotError("JSON 文件必须使用 UTF-8 编码") from exc
        return _load_json_text(raw_text), upload.filename, raw_text

    raw_text = request.form.get("snapshot_text")
    if raw_text is None:
        raw_text = request.form.get("snapshot")
    if raw_text is not None:
        _enforce_original_size_limit(len(raw_text.encode("utf-8")))
        return _load_json_text(raw_text), None, raw_text

    # Last resort: a raw JSON body posted without multipart/form fields.
    if request.data:
        raw_text = request.get_data(as_text=True)
        if raw_text.strip():
            _enforce_original_size_limit(len(raw_text.encode("utf-8")))
            return _load_json_text(raw_text), None, raw_text

    return _load_json_text(None), None, None


def _first_id_value(source: Any = None) -> str | None:
    sources = [source] if source is not None else [request.form, request.args]
    for mapping in sources:
        if mapping is None:
            continue
        for field in ID_FIELDS:
            try:
                value = mapping.get(field)
            except AttributeError:
                continue
            if isinstance(value, str) and snapshot_store.is_token(value.strip()):
                return value.strip()
    return None


def _looks_like_json(text: str) -> bool:
    stripped = text.lstrip()
    return bool(stripped) and stripped[0] in "{["


def _store(snapshot: dict[str, Any]) -> str | None:
    try:
        return snapshot_store.create_snapshot(snapshot)
    except Exception:  # storage must never break the request
        return None


def _resolve_snapshot_reference() -> tuple[dict[str, Any], str | None] | None:
    """Resolve a stored id from the request. ``None`` if no usable id."""

    sid = _first_id_value()
    if sid:
        snapshot = snapshot_store.load_snapshot(sid)
        if snapshot is not None:
            return snapshot, sid
    return None


def _resolve_form_snapshot(
    explicit_id: str | None = None,
) -> tuple[dict[str, Any], str | None]:
    """Resolve the canonical snapshot for /plan and /workbench.

    Accepts, in order:
    1. an explicit/path id or opaque stored-snapshot id field (shared across
       workers, tiny postback);
    2. an inline ``snapshot`` field containing canonical/raw JSON;
    3. an inline ``snapshot`` field that itself is a stored token.
    """

    if explicit_id and snapshot_store.is_token(explicit_id):
        stored = snapshot_store.load_snapshot(explicit_id)
        if stored is not None:
            return stored, explicit_id

    referenced = _resolve_snapshot_reference()
    if referenced is not None:
        return referenced

    raw_text = request.form.get("snapshot")
    if raw_text is None:
        raw_text = request.form.get("snapshot_text")

    if raw_text is not None and raw_text.strip():
        text = raw_text.strip()
        if not _looks_like_json(text):
            if snapshot_store.is_token(text):
                snapshot = snapshot_store.load_snapshot(text)
                if snapshot is not None:
                    return snapshot, text
            raise SnapshotError("快照引用无效或已过期，请重新上传快照")
        payload = _load_json_text(raw_text)
        snapshot = normalize_snapshot(payload)
        return snapshot, _store(snapshot)

    sid = _first_id_value()
    if sid:
        raise SnapshotError("快照已过期或已被清理，请重新上传")
    raise SnapshotError("请先提供并校验快照")


def _render_workbench(
    snapshot: dict[str, Any],
    snapshot_id: str | None,
    *,
    error: str | None = None,
    status: int = 200,
):
    snapshot_json = dumps_snapshot(snapshot)
    pretty_json = dumps_snapshot(snapshot, indent=2)
    return (
        render_template(
            "workbench.html",
            snapshot=snapshot,
            snapshot_id=snapshot_id,
            snapshot_json=snapshot_json,
            pretty_json=pretty_json,
            error=error,
        ),
        status,
    )


def _render_workbench_from_payload(
    payload: Any, *, error: str | None = None, status: int = 200
):
    snapshot = normalize_snapshot(payload)
    snapshot_id = _store(snapshot)
    return _render_workbench(
        snapshot, snapshot_id, error=error, status=status
    )


@bp.get("/")
def index():
    return render_template("index.html", error=None)


@bp.post("/snapshot")
@bp.post("/upload")
def snapshot_upload():
    try:
        payload, _filename, _raw_text = _read_snapshot_from_form()

        # When revalidating the editor content, compare against the validated
        # baseline so edits to either MiB or exact *Bytes fields are honored
        # while untouched values keep exact precision.
        baseline_id = request.form.get("based_on") or _first_id_value()
        baseline = snapshot_store.load_snapshot(baseline_id) if baseline_id else None
        if baseline is not None:
            reconcile_with_baseline(payload, baseline)

        snapshot = normalize_snapshot(payload)
        snapshot_id = _store(snapshot)
        return _render_workbench(snapshot, snapshot_id)
    except SnapshotError as exc:
        return render_template("index.html", error=str(exc)), 400


@bp.post("/workbench")
def workbench_restore():
    try:
        snapshot, snapshot_id = _resolve_form_snapshot()
        return _render_workbench(snapshot, snapshot_id)
    except SnapshotError as exc:
        return render_template("index.html", error=str(exc)), 400


@bp.post("/workbench/<snapshot_id>")
def workbench_restore_by_path(snapshot_id: str):
    try:
        snapshot, resolved_id = _resolve_form_snapshot(snapshot_id)
        return _render_workbench(snapshot, resolved_id)
    except SnapshotError as exc:
        return render_template("index.html", error=str(exc)), 400


@bp.get("/workbench")
def workbench_from_query():
    sid = _first_id_value()
    snapshot = snapshot_store.load_snapshot(sid) if sid else None
    if snapshot is None:
        return render_template("index.html", error="快照已过期或无效，请重新上传"), 404
    return _render_workbench(snapshot, sid)


@bp.get("/workbench/<snapshot_id>")
def workbench_by_id(snapshot_id: str):
    snapshot = snapshot_store.load_snapshot(snapshot_id)
    if snapshot is None:
        return render_template("index.html", error="快照已过期或无效，请重新上传"), 404
    return _render_workbench(snapshot, snapshot_id)


@bp.get("/snapshot/<snapshot_id>")
def snapshot_by_id(snapshot_id: str):
    return workbench_by_id(snapshot_id)


@bp.get("/api/snapshot/<snapshot_id>")
def api_snapshot_by_id(snapshot_id: str):
    snapshot = snapshot_store.load_snapshot(snapshot_id)
    if snapshot is None:
        return jsonify({"ok": False, "error": "快照不存在或已过期"}), 404
    return jsonify({"ok": True, "snapshot_id": snapshot_id, "snapshot": snapshot})


@bp.post("/plan")
@bp.post("/plan/<snapshot_id>")
def plan_form(snapshot_id: str | None = None):
    snapshot: dict[str, Any] | None = None
    resolved_id: str | None = snapshot_id
    try:
        snapshot, resolved_id = _resolve_form_snapshot(snapshot_id)
        failed_node = request.form.get("failed_node") or request.form.get("failedNode")
        if not failed_node and snapshot_id is None:
            # Also accept the node as a query/route-ish alias.
            failed_node = request.args.get("failed_node") or request.args.get("node")
        if not failed_node:
            return _render_workbench(
                snapshot, resolved_id, error="请选择一台故障节点", status=400
            )
        result = plan_recovery(snapshot, failed_node)
        snapshot_json = dumps_snapshot(snapshot)
        return render_template(
            "result.html",
            snapshot=snapshot,
            snapshot_json=snapshot_json,
            snapshot_id=resolved_id,
            result=result,
        )
    except (SnapshotError, RecoveryError) as exc:
        # If a validated snapshot was resolved, keep the user on the
        # workbench with its id; otherwise go back to the upload page.
        if snapshot is not None:
            return _render_workbench(
                snapshot, resolved_id, error=str(exc), status=400
            )
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
            data = json.loads(raw_body, parse_float=Decimal) if raw_body.strip() else None
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

    # Allow {"snapshot_id": "...", "failed_node": "..."} as well.
    payload: Any = data.get("snapshot", data)
    canonical_snapshot: dict[str, Any] | None = None
    if not isinstance(data.get("snapshot"), (dict, list, str)):
        sid = _first_id_value(data)
        if sid:
            canonical_snapshot = snapshot_store.load_snapshot(sid)
        if canonical_snapshot is not None:
            payload = canonical_snapshot

    failed_node = (
        data.get("failed_node")
        or data.get("failedNode")
        or data.get("failed_node_name")
        or data.get("node")
    )
    if isinstance(payload, str):
        try:
            payload = json.loads(payload, parse_float=Decimal)
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
        snapshot = (
            canonical_snapshot
            if canonical_snapshot is not None
            else normalize_snapshot(payload)
        )
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
