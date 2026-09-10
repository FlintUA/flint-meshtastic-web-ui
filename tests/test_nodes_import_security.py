"""Security tests for POST /api/nodes_import input validation and sanitization.
"""


def test_nodes_import_valid_node(server_module):
    client = server_module.app.test_client()

    payload = {
        "nodes": [
            {
                "node_id": "!12345678",
                "name": "Valid Node",
                "short_name": "VALD",
                "hw_model": "TLORA_V2",
                "role": "CLIENT",
            }
        ]
    }

    response = client.post("/api/nodes_import", json=payload)
    assert response.status_code == 200
    data = response.get_json()
    assert data["ok"] is True
    assert data["imported_count"] == 1

    assert "!12345678" in server_module.nodes
    node = server_module.nodes["!12345678"]
    assert node["name"] == "Valid Node"
    assert node["short_name"] == "VALD"
    assert node["hw_model"] == "TLORA_V2"
    assert node["role"] == "CLIENT"


def test_nodes_import_rejects_malformed_and_injection_node_ids(server_module):
    client = server_module.app.test_client()

    payload = {
        "nodes": [
            {"node_id": "12345678", "name": "Missing Exclamation"},
            {"node_id": "!1234567", "name": "Too Short"},
            {"node_id": "!123456789", "name": "Too Long"},
            {"node_id": "!1234567g", "name": "Non Hex"},
            {"node_id": "!12345678/../../etc/passwd", "name": "Path Traversal"},
            {"node_id": "!12345678; rm -rf /", "name": "Command Injection"},
            {"node_id": "!12345678\x00", "name": "Null Byte"},
            {"node_id": None, "name": "None ID"},
            {"node_id": "", "name": "Empty ID"},
        ]
    }

    response = client.post("/api/nodes_import", json=payload)
    assert response.status_code == 200
    data = response.get_json()
    assert data["ok"] is True
    assert data["imported_count"] == 0

    assert len(server_module.nodes) == 0


def test_nodes_import_invalid_payload_format(server_module):
    client = server_module.app.test_client()

    response = client.post("/api/nodes_import", json={"nodes": "not-a-list"})
    assert response.status_code == 400
    data = response.get_json()
    assert data["ok"] is False
    assert data["error_code"] == "invalid_payload"


def test_nodes_import_sanitizes_input_strings(server_module):
    client = server_module.app.test_client()

    payload = {
        "nodes": [
            {
                "node_id": "!87654321",
                "name": "Bad\x00Name\x07With\x00Controls",
                "short_name": "S\x00T",
                "hw_model": "HW\x01Model",
                "role": "CLIENT\x00",
            }
        ]
    }

    response = client.post("/api/nodes_import", json=payload)
    assert response.status_code == 200
    data = response.get_json()
    assert data["imported_count"] == 1

    node = server_module.nodes["!87654321"]
    assert node["name"] == "BadNameWithControls"
    assert node["short_name"] == "ST"
    assert node["hw_model"] == "HWModel"
    assert node["role"] == "CLIENT"
