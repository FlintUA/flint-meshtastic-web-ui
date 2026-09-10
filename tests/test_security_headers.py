"""Tests for HTTP security headers added via @app.after_request in server.py."""


def test_security_headers_present_on_all_responses(server_module):
    app = server_module.app
    client = app.test_client()

    routes = ["/", "/api/sensors", "/api/base_status"]
    for route in routes:
        response = client.get(route)
        headers = response.headers

        assert headers.get("X-Content-Type-Options") == "nosniff"
        assert headers.get("X-Frame-Options") == "SAMEORIGIN"
        assert headers.get("X-XSS-Protection") == "1; mode=block"
        assert headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"
