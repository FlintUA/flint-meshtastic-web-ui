"""Tests for api/api_weather.py error handling and security."""

from unittest.mock import MagicMock
from flask import Flask
import pytest

from api.api_weather import register_weather_routes
import api.api_weather as api_weather_module


@pytest.fixture
def weather_app():
    app = Flask(__name__)
    mock_provider = MagicMock()
    mock_provider.test_api_key.return_value = {"ok": True}

    mock_manager = MagicMock()
    mock_manager.active.return_value = mock_provider
    mock_manager.active_id = "openweather"

    register_weather_routes(
        app,
        weather_manager=mock_manager,
        resolve_location=lambda: {"latitude": 50.0, "longitude": 10.0},
        secrets_path="/dummy/path/weather_secrets.py",
    )
    return app.test_client()


def test_api_weather_save_key_oserror_sanitizes_error_response(weather_app, monkeypatch):
    def mock_save_secret(path, provider_id, api_key):
        raise OSError("[Errno 13] Permission denied: '/app/weather_secrets.py'")

    monkeypatch.setattr(api_weather_module, "_save_weather_secret", mock_save_secret)

    response = weather_app.post(
        "/api/weather/config",
        json={"api_key": "a_valid_dummy_api_key_1234567890"},
    )

    assert response.status_code == 500
    data = response.get_json()
    assert data["ok"] is False
    assert data["error"] == "Could not save the API key."
    # Ensure raw OSError details and internal paths are NOT exposed to the client
    assert "/app/weather_secrets.py" not in data["error"]
    assert "Permission denied" not in data["error"]
