"""
The HTTP twins of the agent tools. Checked structurally (like test_off_loop):
server.py is not imported, so no FastAPI app / projects dir is needed.
"""
from pathlib import Path

SRC = (Path(__file__).resolve().parent.parent / "server.py").read_text()


def _route(decorator: str) -> str:
    assert decorator in SRC, decorator
    return SRC.split(decorator)[1].split("\n@app.")[0]


def test_agent_routes_exist():
    for d in ('@app.get("/api/graph/{name}/compact")',
              '@app.get("/api/graph/{name}/validate")',
              '@app.post("/api/graph/{name}/set_param")',
              '@app.post("/api/graph/{name}/edit_code")',
              '@app.post("/api/graph/{name}/ops")',
              '@app.get("/api/nodes/{node_type}")'):
        _route(d)


def test_execute_keeps_code_for_the_editor_and_is_lean_on_request():
    """nodes.html reads `data.code` (its Code tab) from the default response,
    so only `?lean=1` may drop it."""
    r = _route('@app.post("/api/graph/{name}/execute")')
    assert "lean: bool = False" in r
    assert '"code": result["code"]' in r
    assert "api.summarize_execute" in r
    assert "api.apply_overrides" in r


def test_a_stale_write_is_a_409():
    r = SRC.split("def _agent_call")[1].split("\n@app.")[0]
    assert "StaleGraphError" in r and "HTTPException(409" in r


def test_a_failed_screenshot_is_never_a_200():
    r = _route('@app.get("/api/graph/{name}/screenshot")')
    assert "HTTPException(502" in r


def test_whole_graph_save_returns_its_warnings():
    r = _route('@app.post("/api/graph/{name}")')
    assert '"warnings": warnings' in r
