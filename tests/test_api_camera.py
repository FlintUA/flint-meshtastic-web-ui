"""Tests for api/api_camera.py's screenshot-serving route.

Covers the defense-in-depth consolidation from the stabilization
follow-up (sentinel/fix-path-traversal-screenshot-route-...): the route
used to re-pass the raw `filename` to send_from_directory() after
already validating it via camera.screenshot_exists() -> safe_screenshot_
path() - two independent validation points that only stayed safe
because they happened to agree, not because that agreement was
structurally guaranteed. Live PoC against 5 traversal payloads
independently confirmed the old code was NOT actually exploitable
(safe_screenshot_path() already rejected every payload before
send_from_directory() was ever reached) - this is not a vulnerability
fix, it removes the latent fragility of that two-point validation.

test_api_camera_screenshot_file_path_traversal below goes further than
just asserting 404: it spies on flask.send_file to prove the traversal
payload never reaches the file-serving call at all, rather than merely
observing the same response code a coincidentally-safe implementation
would also produce.
"""

from functools import wraps
from unittest.mock import MagicMock
import pytest
from flask import Flask

from conftest import _stub_libcamera
_stub_libcamera()

from api.api_camera import register_camera_routes
import camera.camera as camera_module
import api.api_camera as api_camera_module


def _handle_errors(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except Exception as error:
            return {"ok": False, "error": str(error)}, 500
    return wrapped


@pytest.fixture
def api_env(tmp_path, monkeypatch):
    screenshots_dir = tmp_path / "screenshots"
    screenshots_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(camera_module, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(camera_module, "SCREENSHOTS_DIR", str(screenshots_dir))

    app = Flask(__name__)
    camera_manager_state = {"manager": None}
    device_manager = MagicMock()

    register_camera_routes(
        app,
        camera_module,
        camera_manager_state,
        device_manager,
        _handle_errors
    )
    return {"app": app, "client": app.test_client(), "screenshots_dir": screenshots_dir, "tmp_path": tmp_path}


def test_api_camera_screenshot_file_valid(api_env):
    screenshots_dir = api_env["screenshots_dir"]
    sub_dir = screenshots_dir / "2025" / "01" / "01"
    sub_dir.mkdir(parents=True, exist_ok=True)
    file_path = sub_dir / "MC_test.jpg"
    file_path.write_bytes(b"fake jpeg data")

    response = api_env["client"].get("/api/camera/screenshot/2025/01/01/MC_test.jpg")
    assert response.status_code == 200
    assert response.data == b"fake jpeg data"


def test_api_camera_screenshot_file_not_found(api_env):
    response = api_env["client"].get("/api/camera/screenshot/nonexistent.jpg")
    assert response.status_code == 404
    assert response.get_json() == {"ok": False, "error": "File not found"}


def test_api_camera_screenshot_file_path_traversal(api_env, monkeypatch):
    """Spies on send_file so a passing test proves the traversal payload
    never reached the file-serving call - not just that the response
    happened to be 404, which a coincidentally-safe implementation could
    also produce without the underlying guarantee actually holding."""
    secret_file = api_env["tmp_path"] / "secret.txt"
    secret_file.write_text("secret data")

    send_file_spy = MagicMock(wraps=api_camera_module.send_file)
    monkeypatch.setattr(api_camera_module, "send_file", send_file_spy)

    for payload in ("../secret.txt", "..%2fsecret.txt", "..\\secret.txt"):
        response = api_env["client"].get(f"/api/camera/screenshot/{payload}")
        assert response.status_code == 404
        assert response.get_json() == {"ok": False, "error": "File not found"}

    send_file_spy.assert_not_called()


def test_api_camera_screenshot_file_serves_the_already_validated_path(api_env, monkeypatch):
    """When a request does succeed, send_file() must receive the resolved,
    validated path that safe_screenshot_path() produced - not the raw,
    attacker-controlled filename string - closing the gap where the two
    could theoretically diverge."""
    screenshots_dir = api_env["screenshots_dir"]
    file_path = screenshots_dir / "plain.jpg"
    file_path.write_bytes(b"data")

    send_file_spy = MagicMock(wraps=api_camera_module.send_file)
    monkeypatch.setattr(api_camera_module, "send_file", send_file_spy)

    response = api_env["client"].get("/api/camera/screenshot/plain.jpg")
    assert response.status_code == 200

    send_file_spy.assert_called_once()
    served_path = send_file_spy.call_args[0][0]
    assert served_path == camera_module.safe_screenshot_path("plain.jpg")


def test_api_camera_screenshot_delete_file_valid(api_env):
    screenshots_dir = api_env["screenshots_dir"]
    sub_dir = screenshots_dir / "2025" / "01" / "01"
    sub_dir.mkdir(parents=True, exist_ok=True)
    file_path = sub_dir / "MC_delete_test.jpg"
    file_path.write_bytes(b"data to delete")

    response = api_env["client"].delete("/api/camera/screenshot/2025/01/01/MC_delete_test.jpg")
    assert response.status_code == 200
    assert response.get_json()["ok"] is True
    assert not file_path.exists()


def test_api_camera_screenshot_delete_directory_returns_404(api_env):
    screenshots_dir = api_env["screenshots_dir"]
    sub_dir = screenshots_dir / "2025" / "01" / "01"
    sub_dir.mkdir(parents=True, exist_ok=True)

    response = api_env["client"].delete("/api/camera/screenshot/2025/01/01")
    assert response.status_code == 404
    assert response.get_json() == {"ok": False, "error": "File not found"}
    assert sub_dir.exists()
