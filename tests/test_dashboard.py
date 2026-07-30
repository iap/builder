"""Offline tests for dashboard/plugin_api.py routes."""
# SPDX-License-Identifier: MIT
from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from fastapi import FastAPI

from dashboard.plugin_api import router


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_status_ok(client):
    with patch("dashboard.plugin_api.get_status", return_value={"authenticated": False, "phase": "idle"}):
        r = client.get("/builder/status")
    assert r.status_code == 200
    assert r.json()["phase"] == "idle"


def test_identity_ok(client):
    with patch("dashboard.plugin_api.show_identity", return_value={"authenticated": False}):
        r = client.get("/builder/identity")
    assert r.status_code == 200
    assert "authenticated" in r.json()


def test_models_ok(client):
    with patch("dashboard.plugin_api.list_models", return_value=["auto", "amazon.nova-pro-v1:0"]):
        r = client.get("/builder/models")
    assert r.status_code == 200
    assert "auto" in r.json()


def test_tags_ok(client):
    with patch("dashboard.plugin_api.load_tags", return_value=["fast", "vision"]):
        r = client.get("/builder/tags")
    assert r.status_code == 200
    assert "fast" in r.json()


def test_status_error(client):
    with patch("dashboard.plugin_api.get_status", side_effect=RuntimeError("boom")):
        r = client.get("/builder/status")
    assert r.status_code == 500
