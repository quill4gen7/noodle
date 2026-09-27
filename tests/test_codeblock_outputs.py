"""CodeBlock named outputs (`#@out`) and block-relative error locations.

Pure-Python: asserts on the parser, the generated source and the runtime
helpers lifted out of the PREAMBLE. The one end-to-end run (a real worker
executing the graph) is skipped where build123d is not installed.
"""

import tempfile
from pathlib import Path

import pytest

from cad_nodes.executor import _codeblock_where, _finalize
from cad_nodes.graph import Graph, ValidationError
from cad_nodes.transpiler import (PREAMBLE, check_codeblock,
                                  parse_codeblock_outputs, transpile)

OUT_CODE = (
    "w = 10  #@param float min=1 max=50\n"
    "#@out body: solid\n"
    "#@out lid solid\n"
    "#@out n: number\n"
    "#@out junk: nonsense\n"
    "#@out result: solid\n"          # reserved -> ignored
    "body = Box(w, 10, 5)\n"
    "lid = Box(w, 10, 1)\n"
    "n = 3\n"
    "result = 42\n"
)


def _cb_graph(code=OUT_CODE, conns=(), extra=()):
    return Graph.from_dict({"name": "t", "nodes": [
        {"id": "cb", "type": "CodeBlock", "params": {"code": code}},
    ] + list(extra), "connections": list(conns)})


def _nodes(code):
    return code[code.index("# --- nodes ---"):]


def _helpers():
    """_CbOuts/_cb_pack/_cb_pick/_cb_where, lifted out of the PREAMBLE."""
    start = PREAMBLE.index("class _CbOuts(dict):")
    end = PREAMBLE.index("def _fanout(")
    src = PREAMBLE[start:end].replace('\\"\\"\\"', '"""')
    ns: dict = {}
    exec(compile(src, "<preamble:cb>", "exec"), ns)
    return ns


# --- parsing ---------------------------------------------------------------
def test_parse_outputs_types_and_aliases():
    outs = parse_codeblock_outputs(OUT_CODE)
    assert outs == [{"name": "body", "type": "solid"},
                    {"name": "lid", "type": "solid"},
                    {"name": "n", "type": "data"},
                    {"name": "junk", "type": "data"}]
    assert parse_codeblock_outputs("result = 1") == []
    assert parse_codeblock_outputs("#@out a\n#@out a: solid") == [
        {"name": "a", "type": "data"}]                     # first declaration wins


# --- graph wiring ----------------------------------------------------------
def test_named_output_validates_and_types():
    g = _cb_graph(conns=[{"id": "c1", "from_node": "cb", "from_socket": "lid",
                          "to_node": "m", "to_socket": "shape"}],
                  extra=[{"id": "m", "type": "Move", "params": {"z": 5}}])
    assert g.validate() == []
    assert g.effective_output_type("cb", "lid") == "solid"
    assert g.effective_output_type("cb", "n") == "data"
    assert g.effective_output_type("cb", "result") == "data"


def test_undeclared_output_rejected():
    g = _cb_graph(conns=[{"id": "c1", "from_node": "cb", "from_socket": "ghost",
                          "to_node": "m", "to_socket": "shape"}],
                  extra=[{"id": "m", "type": "Move", "params": {}}])
    with pytest.raises(ValidationError):
        g.validate()


def test_named_output_connection_round_trips():
    d = {"name": "t", "nodes": [
        {"id": "cb", "type": "CodeBlock", "params": {"code": OUT_CODE},
         "position": [0, 0], "parent": None},
        {"id": "m", "type": "Move", "params": {}, "position": [0, 0], "parent": None}],
        "connections": [{"id": "c1", "from_node": "cb", "from_socket": "body",
                         "to_node": "m", "to_socket": "shape"}]}
    g = Graph.from_dict(d)
    assert g.to_dict()["connections"] == d["connections"]
    assert Graph.from_dict(g.to_dict()).validate() == []


# --- emitted code ----------------------------------------------------------
def test_emit_named_outputs_each_get_a_var():
    g = _cb_graph(conns=[{"id": "c1", "from_node": "cb", "from_socket": "lid",
                          "to_node": "m", "to_socket": "shape"}],
                  extra=[{"id": "m", "type": "Move", "params": {"z": 5}}])
    code = _nodes(transpile(g))
    assert "return _cb_pack(locals(), ('body', 'lid', 'n', 'junk'))" in code
    lid_line = next(ln for ln in code.splitlines() if "_cb_pick(" in ln and "'lid'" in ln)
    lid_var = lid_line.strip().split(" = ")[0]
    assert f"{lid_var} = None" in code          # pre-bound: a failing block leaves None
    move = next(ln for ln in code.splitlines() if "_move(" in ln)
    assert f"_move({lid_var}," in move           # the wire reads THAT socket
    assert "#@out body" in code                  # comment lines kept (line numbers hold)


def test_no_out_is_backward_compatible():
    code = _nodes(transpile(_cb_graph("result = Box(1, 2, 3)")))
    assert "return result" in code and "_cb_pack" not in code and "_cb_pick(" not in code


def test_memo_keys_stable_with_named_outputs():
    def keys(extra_nodes):
        from cad_nodes.transpiler import Transpiler
        t = Transpiler(_cb_graph(extra=extra_nodes), memo=True)
        t.run()
        return t.key_of["cb"]
    # an unrelated node emitted first renumbers every var; the key must not move
    assert keys([]) == keys([{"id": "a0", "type": "Box", "params": {}}])


# --- runtime helpers -------------------------------------------------------
def test_cb_pack_and_pick():
    h = _helpers()
    packed = h["_cb_pack"]({"body": 1, "result": {"lid": 2}}, ("body", "lid", "gone"))
    assert packed == {"result": {"lid": 2}, "body": 1, "lid": 2, "gone": None}
    assert h["_cb_pick"](packed, "lid") == 2
    assert h["_cb_pick"]([packed, packed], "body") == [1, 1]     # fanned block
    assert h["_cb_pick"]("plain", "result") == "plain"


def test_cb_where_maps_back_to_user_line():
    h = _helpers()
    # generated: def line 1, body lines 2..4 = user lines 1, 3, 4 (line 2 was a
    # dropped #@param). The error is on body line 3 -> user line 4.
    src = "def __codeblock_9(k):\n    a = 1\n    b = 2\n    return 1/0\n"
    exec(compile(src, "_run.py", "exec"), h)
    try:
        h["__codeblock_9"](1)
    except ZeroDivisionError as e:
        assert h["_cb_where"](e, "__codeblock_9", (2,), 3) == " (CodeBlock line 4)"
        assert h["_cb_where"](e, "__nope", (), 3) == ""


# --- syntax errors ---------------------------------------------------------
def test_check_codeblock_reports_block_line():
    assert check_codeblock("result = 1") is None
    bad = check_codeblock("k = 2  #@param int\na = 1\nx = (1,\nresult = 2\n")
    assert bad["line"] == 3 and bad["col"] == 5          # the '(' never closed
    bad = check_codeblock("a = 1\n  b = 2\n")
    assert bad["line"] == 2                                # unexpected indent


def test_syntax_error_becomes_a_stub_not_a_broken_program():
    g = Graph.from_dict({"name": "t", "nodes": [
        {"id": "bad", "type": "CodeBlock", "params": {"code": "a = 1\nx = (1,\n"}},
        {"id": "ok", "type": "Box", "params": {}}], "connections": []})
    code = transpile(g)
    body = code[code.index("# --- nodes ---"):]
    compile(body, "_run.py", "exec")          # the program itself stays valid
    assert "raise SyntaxError(" in body and "(CodeBlock line 2, col 5)" in body


def test_executor_surfaces_line_and_col(tmp_path):
    raw = "SyntaxError: '(' was never closed (CodeBlock line 2, col 5)"
    assert _codeblock_where(raw) == {"line": 2, "col": 5}
    assert _codeblock_where("ZeroDivisionError: x (CodeBlock line 7)") == {"line": 7}
    assert _codeblock_where("ValueError: nope") == {}
    view = tmp_path / "view.json"
    view.write_text('{"success": true, "node_errors": {"cb": "%s"}}' % raw.replace("'", "'"))
    res = _finalize("", "", "", None, view, tmp_path / "o.stl")
    assert res["node_errors"]["cb"]["line"] == 2 and res["node_errors"]["cb"]["col"] == 5


# --- end to end (real worker) ---------------------------------------------
def test_end_to_end_named_outputs_and_errors(monkeypatch):
    pytest.importorskip("build123d")
    from cad_nodes import executor
    monkeypatch.setattr(executor, "_warm_enabled", False)
    g = Graph.from_dict({"name": "t", "nodes": [
        {"id": "cb", "type": "CodeBlock", "params": {"code": OUT_CODE}},
        {"id": "m", "type": "Move", "params": {"z": 10}},
        {"id": "bad", "type": "CodeBlock", "params": {"code": "a = 1\nx = (1,\n"}},
        {"id": "rt", "type": "CodeBlock",
         "params": {"code": "k = 2  #@param int\na = 1\n\nresult = 1/0\n"}},
    ], "connections": [{"id": "c1", "from_node": "cb", "from_socket": "lid",
                        "to_node": "m", "to_socket": "shape"}]})
    res = executor.execute_graph(g, Path(tempfile.mkdtemp()))
    assert res["success"]
    errs = res["node_errors"]
    assert errs["bad"]["line"] == 2 and errs["bad"]["col"] == 5
    assert errs["rt"]["line"] == 4 and "ZeroDivisionError" in errs["rt"]["exception"]
    bb = res["view"]["bbox"]                 # the Move got the 1mm lid, raised 10
    assert abs(bb["size"][2] - 1.0) < 0.01 and abs(bb["min"][2] - 9.5) < 0.01
