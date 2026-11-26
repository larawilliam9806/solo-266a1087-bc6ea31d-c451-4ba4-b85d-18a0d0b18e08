"""Kubernetes failure-recovery drill application."""

from __future__ import annotations


def create_app():
    # Imported lazily so the pure solver can be used/tested without Flask.
    from flask import Flask

    from .routes import bp

    app = Flask(
        __name__,
        static_folder="../static",
        template_folder="../templates",
    )
    # Original snapshot size ceiling: first ingestion rejects snapshots
    # larger than 5 MiB (also enforced explicitly in routes).  HTML
    # postback routes relax the per-request body limit so an accepted
    # snapshot carried as an inline hidden field can never turn into a
    # 413 after form encoding; the primary page flow posts a tiny id.
    app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024
    app.config["MAX_SNAPSHOT_BYTES"] = 5 * 1024 * 1024
    app.config["POSTBACK_MAX_CONTENT_LENGTH"] = 128 * 1024 * 1024
    app.json.ensure_ascii = False
    app.register_blueprint(bp)
    return app
