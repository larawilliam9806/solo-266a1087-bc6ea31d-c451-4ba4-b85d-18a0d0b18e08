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
    app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024
    app.json.ensure_ascii = False
    app.register_blueprint(bp)
    return app
