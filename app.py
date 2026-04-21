#!/usr/bin/env python3
"""HTTP wrapper for cron-job.org / Render deployment."""

from __future__ import annotations

import os
from pathlib import Path

from flask import Flask, jsonify, request

from check_myoffice_news import (
    DEFAULT_ENV_PATH,
    exclusive_lock,
    get_lock_path,
    get_state_path,
    load_env,
    run_check,
    send_test_telegram,
)


app = Flask(__name__)
load_env(Path(os.getenv("MYOFFICE_ENV_PATH", str(DEFAULT_ENV_PATH))))


def is_authorized() -> bool:
    expected = os.getenv("RUN_WEBHOOK_TOKEN", "").strip()
    if not expected:
        return False

    auth_header = request.headers.get("Authorization", "").strip()
    if auth_header == f"Bearer {expected}":
        return True

    header_token = request.headers.get("X-Webhook-Token", "").strip()
    if header_token == expected:
        return True

    query_token = request.args.get("token", "").strip()
    if query_token == expected:
        return True

    body = request.get_json(silent=True) or {}
    return str(body.get("token", "")).strip() == expected


@app.get("/")
def index():
    return jsonify(
        {
            "ok": True,
            "service": "check-myoffice-news",
            "routes": ["/healthz", "/run", "/test-telegram"],
        }
    )


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True})


@app.post("/run")
def run_job():
    if not is_authorized():
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    payload = request.get_json(silent=True) or {}
    notify_existing = bool(payload.get("notify_existing", False))
    dry_run = bool(payload.get("dry_run", False))

    try:
        with exclusive_lock(get_lock_path()):
            result = run_check(
                state_path=get_state_path(),
                notify_existing=notify_existing,
                dry_run=dry_run,
            )
    except Exception as exc:  # pragma: no cover - surfaced to caller
        return jsonify({"ok": False, "error": str(exc)}), 500

    return jsonify(
        {
            "ok": True,
            "status": result.status,
            "message": result.message,
            "pending_count": result.pending_count,
            "archived_count": result.archived_count,
            "new_count": result.new_count,
            "first_run": result.first_run,
            "telegram_sent": result.telegram_sent,
            "state_updated": result.state_updated,
        }
    )


@app.post("/test-telegram")
def test_telegram():
    if not is_authorized():
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    try:
        message = send_test_telegram()
    except Exception as exc:  # pragma: no cover - surfaced to caller
        return jsonify({"ok": False, "error": str(exc)}), 500
    return jsonify({"ok": True, "message": message})


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
