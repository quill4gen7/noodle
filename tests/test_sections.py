"""cad_nodes/sections.py — a CodeBlock read as the nodes it contains, unchanged."""
import pytest

from cad_nodes.sections import analyze, compact, instrument
from cad_nodes.transpiler import check_codeblock

HEADERS = '''\
w = 40.0  #@param float min=10 max=80
h = 20.0  #@param float min=5 max=40
# ---- quote ----
half = w / 2
top = h + 2
# ---- helpers ----
def plate(x):
    return Box(x, top, 2)
# ---- shell ----
shell = Box(w, w, h)
shell -= Pos(0, 0, 1) * Box(w - 4, w - 4, h)
# ---- holes ----
for sx in (-1, 1):
    shell -= Pos(sx * half, 0, 0) * Cylinder(1, 10)
# ---- lid ----
lid = plate(w)
# ---- refs ----
for sx in (-1, 1):
    pass
result = [shell, lid, plate(3)]
'''

GROUPED = '''\
L = 100.0  #@param float min=50 max=200
W = 60.0   #@param float min=30 max=90
zt = L / 4
body = Box(L, W, 30)
body = fillet(body.edges(), 2)
lid = Pos(0, 0, zt) * Box(L, W, 2)
parts, ghost = [], []
for sx in (-1, 1):
    parts.append(Pos(sx * L / 2, 0, 0) * Cylinder(5, 4))
ghost.append(Box(10, 10, 10))
spare = Box(1, 1, 1)
dummy = Compound(ghost)
result = body
#@out body: solid
#@out lid: solid
#@out dummy: data
'''


def _by_title(a):
    return {s["title"]: s for s in a["sections"]}


def test_headers_split_the_block_and_find_the_chain():
    a = analyze(HEADERS)
    assert a["headers"] is True
    t = _by_title(a)
    assert list(t) == ["Inizio", "quote", "helpers", "shell", "holes", "lid", "refs"]
    assert t["Inizio"]["kind"] == "params" and t["quote"]["kind"] == "quote"
    assert t["helpers"]["kind"] == "funcs" and t["helpers"]["functions"] == ["plate"]
    assert t["shell"]["kind"] == "part"
    # `shell -= …` inside a loop: a later step on the shell made upstream
    assert t["holes"]["kind"] == "chain" and t["holes"]["chain"] == ["shell"]
    edges = {(e["from"], e["to"]): e["names"] for e in a["edges"]}
    sid = {s["title"]: s["id"] for s in a["sections"]}
    assert edges[(sid["shell"], sid["holes"])] == ["shell"]
    # calling plate() pulls in its free variable `top` from the quote section
    assert "top" in edges[(sid["quote"], sid["lid"])]
    assert "plate" in edges[(sid["helpers"], sid["lid"])]


def test_loop_temporaries_do_not_flow_between_sections():
    a = analyze(HEADERS)
    assert not any("sx" in e["names"] for e in a["edges"])


def test_result_items_point_at_their_sections():
    a = analyze(HEADERS)
    sid = {s["title"]: s["id"] for s in a["sections"]}
    items = {i["expr"]: i["section"] for i in a["result_items"]}
    assert items == {"shell": sid["holes"], "lid": sid["lid"], "plate(3)": sid["helpers"]}


def test_without_headers_statements_group_by_what_they_build():
    a = analyze(GROUPED)
    assert a["headers"] is False
    t = _by_title(a)
    assert {"Parametri", "Quote", "body", "lid", "spare"} <= set(t)
    assert t["body"]["lines"] == 2 + 1          # both body lines, plus `result = body`
    assert t["Quote"]["kind"] == "quote"        # zt = L / 4
    # the loop that fills the lists, `ghost.append(...)` and `dummy = Compound(ghost)`
    # are ONE group, named after the list read most afterwards
    lists = next(s for s in a["sections"] if s["id"] == a["outputs"]["dummy"])
    assert lists["title"] in ("parts", "ghost")
    assert lists["ranges"] == [[7, 10], [12, 12]]           # `spare` on line 11 sits between
    assert t["spare"]["unused"] is True and t["body"]["unused"] is False
    assert a["outputs"]["body"] == t["body"]["id"]


def test_result_assignment_is_not_a_dimension():
    a = analyze(GROUPED)
    quote = _by_title(a)["Quote"]
    assert all(not (a0 <= 13 <= b0) for a0, b0 in quote["ranges"])     # `result = body` is line 13


@pytest.mark.parametrize("code", [HEADERS, GROUPED])
def test_instrumented_copy_compiles_and_marks_every_range(code):
    a = analyze(code)
    ins = instrument(code, a)
    assert check_codeblock(ins) is None
    n_ranges = sum(len(s["ranges"]) for s in a["sections"])
    assert ins.count("__sec_mark__(") == n_ranges + 1          # + '__start__'
    assert code.splitlines()[0] in ins and "#@param" in ins     # params untouched


def test_bad_code_is_one_section_with_the_error():
    a = analyze("x = (\n")
    assert a["error"] and len(a["sections"]) == 1 and a["edges"] == []


def test_compact_drops_private_and_empty_fields():
    c = compact(analyze(HEADERS))
    for s in c["sections"]:
        assert not any(k.startswith("_") for k in s)
        assert isinstance(s["ranges"], list) and all(len(r) == 2 for r in s["ranges"])
