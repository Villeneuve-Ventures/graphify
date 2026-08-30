"""Tests for the Streamable HTTP transport on the MCP server (issue #1143).

These exercise the ASGI wiring in-process (no uvicorn, no real socket) via
Starlette's TestClient, so they stay fast and offline. The stdio path is
unchanged and covered elsewhere.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("mcp")
pytest.importorskip("starlette")

from starlette.testclient import TestClient  # noqa: E402

from graphify import serve as serve_mod  # noqa: E402

SAMPLE_GRAPH = {
    "directed": True,
    "nodes": [
        {"id": "a", "label": "Alpha", "community": 0},
        {"id": "b", "label": "Beta", "community": 0},
    ],
    "edges": [
        {"source": "a", "target": "b", "relation": "calls", "confidence": "EXTRACTED"},
    ],
}

_INIT_BODY = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}

_MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


def _graph_file(tmp_path: Path) -> str:
    p = tmp_path / "graph.json"
    p.write_text(json.dumps(SAMPLE_GRAPH), encoding="utf-8")
    return str(p)


def _client(app) -> TestClient:
    # Default host is 127.0.0.1, so the DNS-rebinding guard only accepts that
    # Host header (TestClient otherwise sends the disallowed "testserver").
    return TestClient(app, base_url="http://127.0.0.1")


def test_app_builds_and_initialize_succeeds(tmp_path):
    app = serve_mod._build_http_app(_graph_file(tmp_path), json_response=True)
    with _client(app) as client:
        resp = client.post("/mcp", headers=_MCP_HEADERS, json=_INIT_BODY)
        assert resp.status_code == 200
        # json_response=True returns a single JSON-RPC envelope.
        payload = resp.json()
        assert payload["jsonrpc"] == "2.0"
        assert payload["result"]["serverInfo"]["name"] == "graphify"


def test_unknown_path_is_404(tmp_path):
    app = serve_mod._build_http_app(_graph_file(tmp_path), json_response=True)
    with _client(app) as client:
        resp = client.post("/nope", headers=_MCP_HEADERS, json=_INIT_BODY)
        assert resp.status_code == 404


def test_api_key_missing_is_401(tmp_path):
    app = serve_mod._build_http_app(_graph_file(tmp_path), api_key="s3cret", json_response=True)
    with _client(app) as client:
        resp = client.post("/mcp", headers=_MCP_HEADERS, json=_INIT_BODY)
        assert resp.status_code == 401
        assert resp.json()["error"] == "unauthorized"


def test_api_key_wrong_is_401(tmp_path):
    app = serve_mod._build_http_app(_graph_file(tmp_path), api_key="s3cret", json_response=True)
    with _client(app) as client:
        resp = client.post(
            "/mcp",
            headers={**_MCP_HEADERS, "Authorization": "Bearer nope"},
            json=_INIT_BODY,
        )
        assert resp.status_code == 401


def test_api_key_bearer_ok(tmp_path):
    app = serve_mod._build_http_app(_graph_file(tmp_path), api_key="s3cret", json_response=True)
    with _client(app) as client:
        resp = client.post(
            "/mcp",
            headers={**_MCP_HEADERS, "Authorization": "Bearer s3cret"},
            json=_INIT_BODY,
        )
        assert resp.status_code == 200
        assert resp.json()["result"]["serverInfo"]["name"] == "graphify"


def test_api_key_x_api_key_header_ok(tmp_path):
    app = serve_mod._build_http_app(_graph_file(tmp_path), api_key="s3cret", json_response=True)
    with _client(app) as client:
        resp = client.post(
            "/mcp",
            headers={**_MCP_HEADERS, "X-API-Key": "s3cret"},
            json=_INIT_BODY,
        )
        assert resp.status_code == 200


def test_blank_api_key_means_no_auth(tmp_path):
    # An empty/whitespace key must normalize to "no auth", not a key of "".
    app = serve_mod._build_http_app(_graph_file(tmp_path), api_key="   ", json_response=True)
    with _client(app) as client:
        resp = client.post("/mcp", headers=_MCP_HEADERS, json=_INIT_BODY)
        assert resp.status_code == 200


def test_api_key_bearer_scheme_case_insensitive(tmp_path):
    app = serve_mod._build_http_app(_graph_file(tmp_path), api_key="s3cret", json_response=True)
    with _client(app) as client:
        resp = client.post(
            "/mcp",
            headers={**_MCP_HEADERS, "Authorization": "bearer s3cret"},
            json=_INIT_BODY,
        )
        assert resp.status_code == 200


def test_custom_mount_path(tmp_path):
    app = serve_mod._build_http_app(_graph_file(tmp_path), path="/graph", json_response=True)
    with _client(app) as client:
        ok = client.post("/graph", headers=_MCP_HEADERS, json=_INIT_BODY)
        assert ok.status_code == 200
        missing = client.post("/mcp", headers=_MCP_HEADERS, json=_INIT_BODY)
        assert missing.status_code == 404


def test_tools_list_over_http(tmp_path):
    """A full initialize -> tools/list round trip works over the HTTP transport."""
    app = serve_mod._build_http_app(_graph_file(tmp_path), json_response=True)
    with _client(app) as client:
        init = client.post("/mcp", headers=_MCP_HEADERS, json=_INIT_BODY)
        assert init.status_code == 200
        session_id = init.headers.get("mcp-session-id")
        assert session_id, "stateful transport should return a session id"
        notify_headers = {**_MCP_HEADERS, "mcp-session-id": session_id}
        client.post(
            "/mcp",
            headers=notify_headers,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        resp = client.post(
            "/mcp",
            headers=notify_headers,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        assert resp.status_code == 200
        names = {t["name"] for t in resp.json()["result"]["tools"]}
        assert {"query_graph", "get_node", "graph_stats"} <= names


def _project_with_graph(tmp_path, node_count: int) -> str:
    """Create ``<proj>/graphify-out/graph.json`` and return the project dir."""
    proj = tmp_path / "proj"
    (proj / "graphify-out").mkdir(parents=True)
    graph = {
        "directed": True,
        "nodes": [{"id": f"n{i}", "label": f"N{i}", "community": 0} for i in range(node_count)],
        "edges": [],
    }
    (proj / "graphify-out" / "graph.json").write_text(json.dumps(graph), encoding="utf-8")
    return str(proj)


def _init_session(client) -> dict:
    init = client.post("/mcp", headers=_MCP_HEADERS, json=_INIT_BODY)
    assert init.status_code == 200
    headers = {**_MCP_HEADERS, "mcp-session-id": init.headers.get("mcp-session-id")}
    client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    return headers


def _call_tool(client, headers, name, arguments, rid) -> str:
    resp = client.post("/mcp", headers=headers, json={
        "jsonrpc": "2.0", "id": rid, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    })
    assert resp.status_code == 200
    return resp.json()["result"]["content"][0]["text"]


def test_pending_managed_graph_is_rejected_by_http_tool(tmp_path):
    from graphify.transaction import begin_transaction

    graph_path = Path(_graph_file(tmp_path))
    begin_transaction("runtime", tmp_path, output=graph_path.parent)
    app = serve_mod._build_http_app(str(graph_path), json_response=True)
    with _client(app) as client:
        headers = _init_session(client)
        result = _call_tool(client, headers, "graph_stats", {}, rid=2)
    assert "Error executing graph_stats" in result
    assert "receipt" in result.lower()


def test_project_path_is_optional_on_every_tool(tmp_path):
    """Multi-project support is additive: every tool gains an optional
    project_path, and none of them makes it required."""
    app = serve_mod._build_http_app(_graph_file(tmp_path), json_response=True)
    with _client(app) as client:
        headers = _init_session(client)
        resp = client.post("/mcp", headers=headers,
                            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        for tool in resp.json()["result"]["tools"]:
            props = tool["inputSchema"].get("properties", {})
            assert "project_path" in props, f"{tool['name']} missing project_path"
            assert "project_path" not in tool["inputSchema"].get("required", [])


def test_project_path_routes_to_that_projects_graph(tmp_path):
    """One running server answers against the default graph when project_path is
    omitted, and against a project's own graph when it is supplied."""
    proj = _project_with_graph(tmp_path, node_count=3)  # default graph has 2 nodes
    app = serve_mod._build_http_app(_graph_file(tmp_path), json_response=True)
    with _client(app) as client:
        headers = _init_session(client)
        assert "Nodes: 2" in _call_tool(client, headers, "graph_stats", {}, rid=2)
        assert "Nodes: 3" in _call_tool(client, headers, "graph_stats", {"project_path": proj}, rid=3)
        # Falling back to the default afterwards still works (no state leak).
        assert "Nodes: 2" in _call_tool(client, headers, "graph_stats", {}, rid=4)


def test_bad_project_path_errors_without_killing_server(tmp_path):
    """A missing project graph is a tool error, not a process exit — the server
    keeps serving the default graph."""
    app = serve_mod._build_http_app(_graph_file(tmp_path), json_response=True)
    with _client(app) as client:
        headers = _init_session(client)
        bad = _call_tool(client, headers, "graph_stats",
                         {"project_path": str(tmp_path / "does-not-exist")}, rid=2)
        assert "not found" in bad.lower()
        assert "Nodes: 2" in _call_tool(client, headers, "graph_stats", {}, rid=3)


def test_cached_context_is_fenced_when_protocol_becomes_pending(tmp_path):
    from graphify.transaction import begin_transaction

    graph_path = _graph_file(tmp_path)
    root = tmp_path / "corpus"
    root.mkdir()
    app = serve_mod._build_http_app(graph_path, json_response=True)
    with _client(app) as client:
        headers = _init_session(client)
        assert "Nodes: 2" in _call_tool(
            client, headers, "graph_stats", {}, rid=2
        )
        before = Path(graph_path).stat()
        begin_transaction("runtime", root, output=tmp_path)
        after = Path(graph_path).stat()
        assert (before.st_mtime_ns, before.st_size) == (
            after.st_mtime_ns,
            after.st_size,
        )
        blocked = _call_tool(client, headers, "graph_stats", {}, rid=3)
        assert "protocol" in blocked.lower() or "pending" in blocked.lower()


def test_cached_context_refreshes_when_receipt_artifacts_change_without_graph_change(
    tmp_path, monkeypatch
):
    import graphify.transaction as transaction_module

    graph_path = Path(_graph_file(tmp_path))
    payload = graph_path.read_bytes()
    base = transaction_module.GraphSnapshot(
        data=json.loads(payload),
        generation=1,
        graph_path=graph_path,
        payload=payload,
        digest="a" * 64,
        artifacts={"GRAPH_REPORT.md": b"old report"},
        receipt_digest="b" * 64,
    )
    changed = transaction_module.GraphSnapshot(
        data=json.loads(payload),
        generation=2,
        graph_path=graph_path,
        payload=payload,
        digest=base.digest,
        artifacts={"GRAPH_REPORT.md": b"new report"},
        receipt_digest="c" * 64,
    )
    calls = 0

    def snapshots(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return base if calls == 1 else changed

    monkeypatch.setattr(transaction_module, "open_graph_snapshot", snapshots)
    app = serve_mod._build_http_app(str(graph_path), json_response=True)
    with _client(app) as client:
        headers = _init_session(client)
        response = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "resources/read",
                "params": {"uri": "graphify://report"},
            },
        )
    assert response.status_code == 200
    assert response.json()["result"]["contents"][0]["text"] == "new report"


def test_cached_context_refreshes_uncached_learning_overlay(tmp_path, monkeypatch):
    import graphify.reflect as reflect_module

    overlays = iter(
        (
            {"a": {"status": "contested", "stale": False}},
            {"a": {"status": "preferred", "stale": False}},
        )
    )
    monkeypatch.setattr(
        reflect_module,
        "load_learning_overlay",
        lambda _path: next(overlays),
    )
    app = serve_mod._build_http_app(_graph_file(tmp_path), json_response=True)
    with _client(app) as client:
        headers = _init_session(client)
        result = _call_tool(
            client,
            headers,
            "query_graph",
            {"question": "Alpha"},
            rid=2,
        )
    assert "learning=preferred" in result
    assert "learning=contested" not in result


def test_stateless_mode_initialize(tmp_path):
    app = serve_mod._build_http_app(_graph_file(tmp_path), stateless=True, json_response=True)
    with _client(app) as client:
        resp = client.post("/mcp", headers=_MCP_HEADERS, json=_INIT_BODY)
        assert resp.status_code == 200


def test_stateless_with_timeout_does_not_raise(tmp_path):
    # session_timeout must be forced to None in stateless mode (the SDK raises
    # RuntimeError otherwise). Building + a request should just work.
    app = serve_mod._build_http_app(
        _graph_file(tmp_path), stateless=True, session_timeout=3600, json_response=True
    )
    with _client(app) as client:
        assert client.post("/mcp", headers=_MCP_HEADERS, json=_INIT_BODY).status_code == 200


def test_session_timeout_zero_disables(tmp_path):
    # 0 / non-positive must disable reaping without tripping the SDK's validation.
    app = serve_mod._build_http_app(_graph_file(tmp_path), session_timeout=0, json_response=True)
    with _client(app) as client:
        assert client.post("/mcp", headers=_MCP_HEADERS, json=_INIT_BODY).status_code == 200


# --- CLI argument parsing -------------------------------------------------

def test_cli_defaults_to_stdio(monkeypatch):
    calls = {}
    monkeypatch.setattr(serve_mod, "serve", lambda gp: calls.setdefault("stdio", gp))
    monkeypatch.setattr(
        serve_mod, "serve_http", lambda *a, **k: calls.setdefault("http", (a, k))
    )
    serve_mod._main(["graphify-out/graph.json"])
    assert calls.get("stdio") == "graphify-out/graph.json"
    assert "http" not in calls


def test_cli_http_passes_flags(monkeypatch):
    captured = {}
    monkeypatch.setattr(serve_mod, "serve", lambda gp: captured.setdefault("stdio", gp))
    monkeypatch.setattr(
        serve_mod, "serve_http", lambda gp, **k: captured.update(gp=gp, **k)
    )
    serve_mod._main([
        "g.json", "--transport", "http", "--host", "0.0.0.0",
        "--port", "9000", "--api-key", "k", "--stateless",
    ])
    assert captured["gp"] == "g.json"
    assert captured["host"] == "0.0.0.0"
    assert captured["port"] == 9000
    assert captured["api_key"] == "k"
    assert captured["stateless"] is True


def test_cli_api_key_from_env(monkeypatch):
    captured = {}
    monkeypatch.setenv("GRAPHIFY_API_KEY", "from-env")
    monkeypatch.setattr(serve_mod, "serve_http", lambda gp, **k: captured.update(**k))
    serve_mod._main(["g.json", "--transport", "http"])
    assert captured["api_key"] == "from-env"
