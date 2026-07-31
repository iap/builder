"""Offline tests for dashboard/plugin_api.py routes."""
# SPDX-License-Identifier: MIT
from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dashboard.plugin_api import router


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_status_ok(client):
    fake_status = {"authenticated": False, "phase": "idle"}
    with patch("dashboard.plugin_api._sso") as mock_sso:
        mock_sso.return_value.get_status.return_value = fake_status
        r = client.get("/status")
    assert r.status_code == 200
    assert r.json() == fake_status


def test_login_ok(client):
    fake_info = {
        "success": True,
        "user_code": "ABCD-1234",
        "verification_uri": "https://example.com",
        "verification_uri_complete": "https://example.com/complete",
        "expires_in": 300,
        "interval": 5,
        "message": "ok",
    }
    with patch("dashboard.plugin_api._sso") as mock_sso:
        mock_sso.return_value.start_login.return_value = fake_info
        r = client.post("/login")
    assert r.status_code == 200
    assert r.json()["success"] is True
    assert r.json()["user_code"] == "ABCD-1234"


def test_logout_ok(client):
    with patch("dashboard.plugin_api._sso") as mock_sso:
        mock_sso.return_value.logout.return_value = None
        r = client.post("/logout")
    assert r.status_code == 200
    assert r.json()["success"] is True


def test_status_error_returns_internal_error(client):
    with patch("dashboard.plugin_api._sso") as mock_sso:
        mock_sso.return_value.get_status.side_effect = RuntimeError("boom")
        r = client.get("/status")
    assert r.status_code == 200
    body = r.json()
    assert body["authenticated"] is False
    assert body["phase"] == "error"
    assert body["error"] == "internal_error"
