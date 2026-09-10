import pytest
from flask import Flask, jsonify


@pytest.fixture
def security_test_app(server_module):
    app = Flask(__name__)
    app.after_request(server_module.add_security_headers)

    @app.route("/test-success")
    def success_route():
        return jsonify({"ok": True})

    @app.route("/test-error")
    @server_module.handle_errors
    def error_route():
        raise RuntimeError("Internal database failure")

    return app


def test_security_headers_present(security_test_app):
    client = security_test_app.test_client()
    response = client.get("/test-success")

    assert response.status_code == 200
    assert response.headers.get("X-Content-Type-Options") == "nosniff"
    assert response.headers.get("X-Frame-Options") == "SAMEORIGIN"
    assert response.headers.get("X-XSS-Protection") == "1; mode=block"
    assert response.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"


def test_handle_errors_omits_traceback(security_test_app):
    client = security_test_app.test_client()
    response = client.get("/test-error")

    assert response.status_code == 500
    data = response.get_json()
    assert data["ok"] is False
    assert "error" in data
    assert "traceback" not in data
