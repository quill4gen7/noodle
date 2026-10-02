"""scripts/build_pages.py — the static preview keeps working as view.html evolves."""
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("build_pages", ROOT / "scripts" / "build_pages.py")
bp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bp)


def test_view_html_still_patches_cleanly():
    html = bp._patch_view((ROOT / "webui" / "view.html").read_text())
    assert "new URLSearchParams(location.search)" in html          # ?g=…&gen=…
    assert '"/static/' not in html and "'/static/" not in html     # relative assets
    assert "location.pathname + location.search" in html           # hash keeps the query
    assert "`/view/" not in html                                    # no server routes left
    assert html.index("window.fetch = ") < html.index('<script type="importmap">')


def test_build_writes_a_self_contained_site(tmp_path):
    gen = tmp_path / "projects" / "demo" / "gens" / "g1"
    gen.mkdir(parents=True)
    (gen / "view.json").write_text(json.dumps({"previews": {}}))
    (gen / "graph.json").write_text(json.dumps({"nodes": [], "connections": []}))
    (gen / "meta.json").write_text(json.dumps({"gen": "g1", "graph": "demo", "version": "v1"}))
    out = tmp_path / "site"
    bp.build(tmp_path / "projects", out, ["demo/g1"])
    for f in ("demo/index.html", "static/viewer.js", "static/vendor/three-0.170.0/build/three.module.min.js",
              "data/demo/g1/view.json", "data/demo/gens.json", "data/demo/version.json", ".nojekyll"):
        assert (out / f).is_file(), f
    assert json.loads((out / "data/demo/version.json").read_text()) == {"version": "v1"}
    assert "'/static/" not in (out / "static/viewer.js").read_text()
