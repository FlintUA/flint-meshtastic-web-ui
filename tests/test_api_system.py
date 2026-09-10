"""Tests for api/api_system.py's Wi-Fi scan route.

Covers the UX-gap fix: a failed scan (helper-level reason, or an
unexpected exception mid-parse) must (1) surface its real reason string
in the JSON response, unchanged behavior already present, and (2) now
also record a log_system_event() entry with source="wifi", the missing
half of the fix - the frontend's own display path is exercised in
static/chat.js only, not testable from here.
"""
import sys
import types
from unittest.mock import MagicMock

import pytest
from flask import Flask

if "config" not in sys.modules:
    _fake_config = types.ModuleType("config")
    _fake_config.DATA_DIR = "/tmp/meshcenter_test_data"
    sys.modules["config"] = _fake_config

from api.api_system import register_system_routes
import api.api_system as api_system_module
from meshsrv import network_config


@pytest.fixture
def client(monkeypatch):
    app = Flask(__name__)
    register_system_routes(app)

    monkeypatch.setattr(api_system_module, "get_system_events", lambda **k: [])

    return app.test_client()


def test_wifi_scan_helper_failure_returns_reason_and_logs_it(client, monkeypatch):
    logged = []
    monkeypatch.setattr(
        api_system_module, "log_system_event",
        lambda title, level="INFO", details="", source="system": logged.append(
            {"title": title, "level": level, "details": details, "source": source}
        ),
    )
    monkeypatch.setattr(
        network_config, "scan",
        lambda: {"ok": False, "reason": "sudo is not configured for meshcenter-network-helper"},
    )

    response = client.get("/api/system/wifi/scan")

    assert response.status_code == 500
    data = response.get_json()
    assert data["ok"] is False
    assert data["error"] == "sudo is not configured for meshcenter-network-helper"

    assert len(logged) == 1
    assert logged[0]["source"] == "wifi"
    assert logged[0]["level"] == "ERROR"
    assert "sudo is not configured for meshcenter-network-helper" in logged[0]["details"]


def test_wifi_scan_unexpected_exception_returns_reason_and_logs_it(client, monkeypatch):
    logged = []
    monkeypatch.setattr(
        api_system_module, "log_system_event",
        lambda title, level="INFO", details="", source="system": logged.append(
            {"title": title, "level": level, "details": details, "source": source}
        ),
    )

    def _raise():
        raise RuntimeError("iw not found")

    monkeypatch.setattr(network_config, "scan", _raise)

    response = client.get("/api/system/wifi/scan")

    assert response.status_code == 500
    data = response.get_json()
    assert data["ok"] is False
    assert data["error"] == "iw not found"

    assert len(logged) == 1
    assert logged[0]["source"] == "wifi"
    assert logged[0]["level"] == "ERROR"
    assert "iw not found" in logged[0]["details"]


@pytest.mark.parametrize("action,expected_title", [
    ("reboot", "Device reboot requested"),
    ("shutdown", "Device shutdown requested"),
])
def test_system_action_labels_are_generic_not_raspberry_pi(client, monkeypatch, action, expected_title):
    # These used to hardcode "Raspberry Pi reboot"/"Raspberry Pi shutdown" -
    # wrong on any non-Pi host (e.g. a Droidian phone) this project also
    # supports. Also spawns a background thread on success - stub it out so
    # the test doesn't actually try to sudo systemctl reboot/poweroff.
    logged = []
    monkeypatch.setattr(
        api_system_module, "log_system_event",
        lambda title, level="INFO", details="", source="system": logged.append(
            {"title": title, "level": level, "details": details, "source": source}
        ),
    )
    monkeypatch.setattr(api_system_module, "get_device_model", lambda: "")
    monkeypatch.setattr(api_system_module.threading, "Thread", MagicMock())

    response = client.post("/api/system/action", json={"action": action})

    assert response.status_code == 202
    assert response.get_json()["message"] == expected_title
    assert len(logged) == 1
    assert logged[0]["title"] == expected_title
    assert "Raspberry Pi" not in logged[0]["title"]
    assert "Raspberry Pi" not in logged[0]["details"]


def test_system_action_log_includes_device_model_when_available(client, monkeypatch):
    logged = []
    monkeypatch.setattr(
        api_system_module, "log_system_event",
        lambda title, level="INFO", details="", source="system": logged.append(
            {"title": title, "level": level, "details": details, "source": source}
        ),
    )
    monkeypatch.setattr(api_system_module, "get_device_model", lambda: "Google Inc. MSM sdm670 B4 PVT v1.0")
    monkeypatch.setattr(api_system_module.threading, "Thread", MagicMock())

    client.post("/api/system/action", json={"action": "reboot"})

    assert logged[0]["details"] == "Requested from MeshCenter web interface (Google Inc. MSM sdm670 B4 PVT v1.0)"


def test_system_action_log_omits_model_suffix_when_unavailable(client, monkeypatch):
    logged = []
    monkeypatch.setattr(
        api_system_module, "log_system_event",
        lambda title, level="INFO", details="", source="system": logged.append(
            {"title": title, "level": level, "details": details, "source": source}
        ),
    )
    monkeypatch.setattr(api_system_module, "get_device_model", lambda: "")
    monkeypatch.setattr(api_system_module.threading, "Thread", MagicMock())

    client.post("/api/system/action", json={"action": "reboot"})

    assert logged[0]["details"] == "Requested from MeshCenter web interface"


def test_wifi_scan_success_path_does_not_log(client, monkeypatch):
    logged = []
    monkeypatch.setattr(
        api_system_module, "log_system_event",
        lambda *a, **k: logged.append((a, k)),
    )
    monkeypatch.setattr(network_config, "scan", lambda: {"ok": True, "stdout": ""})
    monkeypatch.setattr(network_config, "list_wifi_connections", lambda: {"ok": True, "ssids": set()})

    response = client.get("/api/system/wifi/scan")

    assert response.status_code == 200
    data = response.get_json()
    assert data["ok"] is True
    assert data["networks"] == []
    assert logged == []


def test_system_info_model_field_uses_get_device_model(client, monkeypatch):
    monkeypatch.setattr(api_system_module, "get_device_model", lambda: "Google Inc. MSM sdm670 B4 PVT v1.0")

    response = client.get("/api/system/info")

    assert response.status_code == 200
    assert response.get_json()["model"] == "Google Inc. MSM sdm670 B4 PVT v1.0"


def test_system_info_model_field_is_none_when_unavailable(client, monkeypatch):
    monkeypatch.setattr(api_system_module, "get_device_model", lambda: "")

    response = client.get("/api/system/info")

    assert response.get_json()["model"] is None


@pytest.mark.parametrize("endpoint,method", [
    ("/api/system/wifi/connect", "post"),
    ("/api/system/wifi/forget", "post"),
    ("/api/system/action", "post"),
    ("/api/schedules", "post"),
    ("/api/schedules/1", "put"),
    ("/api/timers", "post"),
])
def test_invalid_json_payload_returns_400(client, endpoint, method):
    tester = getattr(client, method)
    response = tester(endpoint, data="[1, 2, 3]", content_type="application/json")
    assert response.status_code == 400
    data = response.get_json()
    assert data["ok"] is False or "error" in data
    assert "error" in data
