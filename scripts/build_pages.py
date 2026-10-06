"""Build the static preview site (GitHub Pages) from frozen generations.

    python scripts/build_pages.py --projects projects --out site \\
        cassone-demo/g1 threaded-jar-pour/g1 ...

A generation (projects/<name>/gens/gN/{view,graph,meta}.json) is everything the
read-only viewer needs, so /view runs with no server at all: this copies
webui/view.html, viewer.js and the vendored three.js, and points the page's few
API reads at the copied JSON files. The viewer code is the one in the repo —
the demo cannot drift from the app.

Output:
    site/index.html            the landing page (scripts/pages/index.html, thumbs/)
    site/assets/               docs/asset media (GIFs, screenshots)
    site/demo/index.html       /view, reading ?g=<name>&gen=<gN>
    site/static/               viewer.js, icon.svg, vendor/three-0.170.0
    site/data/<name>/          gens.json, version.json, <gen>/{view,graph,meta}.json
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEBUI = ROOT / "webui"

# Runs before the page's module: the read-only viewer asks the API for four
# things; on Pages they are files next to the demo.
FETCH_SHIM = """<script>
(() => {
  const real = window.fetch.bind(window);
  const map = url => {
    let m = /^\\/api\\/graph\\/([^/]+)\\/gens\\/(g\\d+)\\/(view|graph|meta)$/.exec(url);
    if (m) return `../data/${m[1]}/${m[2]}/${m[3]}.json`;
    m = /^\\/api\\/graph\\/([^/]+)\\/(gens|version)$/.exec(url);
    if (m) return `../data/${m[1]}/${m[2]}.json`;
    m = /^\\/api\\/gens\\/recent\\?.*project=([^&]+)/.exec(url);
    if (m) return `../data/${m[1]}/gens.json`;
    return url;
  };
  window.fetch = (input, init) => real(typeof input === 'string' ? map(input) : input, init);
})();
</script>
"""


def _patch_view(html: str) -> str:
    def sub(old: str, new: str, count: int = 1) -> None:
        nonlocal html
        if html.count(old) < 1:
            raise SystemExit(f"view.html changed, cannot patch: {old!r}")
        html = html.replace(old, new) if count == 0 else html.replace(old, new, count)

    sub('"/static/', '"../static/', 0)
    sub("'/static/", "'../static/", 0)
    sub('href="/static/icon.svg"', 'href="../static/icon.svg"', 0) if 'href="/static/icon.svg"' in html else None
    # where the page is: ?g=<name>&gen=<gN> instead of /view/<name>/<gN>
    sub("const seg = location.pathname.split('/').filter(Boolean);     // ['view', name, gen?]",
        "const _q = new URLSearchParams(location.search);\n"
        "const seg = ['view', _q.get('g') || '', _q.get('gen') || ''];   // static preview")
    sub("history.replaceState(null, '', `/view/${encodeURIComponent(NAME)}/${GEN}${location.hash}`);",
        "history.replaceState(null, '', `?g=${encodeURIComponent(NAME)}&gen=${GEN}${location.hash}`);")
    sub("location.href = `/view/${encodeURIComponent(NAME)}/${gen}`;",
        "location.href = `?g=${encodeURIComponent(NAME)}&gen=${gen}`;")
    # no /views gallery on a static site: back to the landing page
    sub("$('all').href = $('m-all').href = '/views?p=' + encodeURIComponent(NAME);",
        "$('all').href = $('m-all').href = '../';")
    # the hash writer keeps ?g=…&gen=… (it used to rebuild the URL from the path alone)
    sub("history.replaceState(null, '', location.pathname + (ps.length ? '#' + ps.join('&') : ''));",
        "history.replaceState(null, '', location.pathname + location.search + (ps.length ? '#' + ps.join('&') : ''));")
    # no editor behind a static page: the button leads to the project instead
    sub("$('edit').href = $('m-edit').href = '/nodes?p=' + encodeURIComponent(NAME);",
        "$('edit').href = $('m-edit').href = 'https://github.com/rederyk/noodle';")
    return html.replace("<head>", "<head>\n" + FETCH_SHIM, 1)


def build(projects: Path, out: Path, gens: list[str]) -> None:
    if out.exists():
        shutil.rmtree(out)
    (out / "demo").mkdir(parents=True)
    (out / "static").mkdir()
    (out / "demo" / "index.html").write_text(_patch_view((WEBUI / "view.html").read_text()))
    viewer = (WEBUI / "viewer.js").read_text()
    viewer = re.sub(r"(['\"])/static/", r"\1../static/", viewer)
    (out / "static" / "viewer.js").write_text(viewer)
    shutil.copy(WEBUI / "icon.svg", out / "static" / "icon.svg")
    shutil.copytree(WEBUI / "vendor" / "three-0.170.0", out / "static" / "vendor" / "three-0.170.0")
    # landing page + media
    pages = ROOT / "scripts" / "pages"
    if (pages / "index.html").exists():
        shutil.copy(pages / "index.html", out / "index.html")
    if (pages / "thumbs").is_dir():
        shutil.copytree(pages / "thumbs", out / "thumbs")
    shutil.copytree(ROOT / "docs" / "asset", out / "assets")
    shutil.copy(WEBUI / "logo.svg", out / "assets" / "logo.svg")
    by_project: dict[str, list[dict]] = {}
    for ref in gens:
        name, gen = ref.split("/")
        src = projects / name / "gens" / gen
        dst = out / "data" / name / gen
        dst.mkdir(parents=True)
        for part in ("view", "graph", "meta"):
            shutil.copy(src / f"{part}.json", dst / f"{part}.json")
        by_project.setdefault(name, []).append(json.loads((src / "meta.json").read_text()))
    for name, metas in by_project.items():
        metas.sort(key=lambda m: int(m["gen"][1:]), reverse=True)
        for m in metas:              # nothing to upload a card picture to
            m["thumb"] = True
        (out / "data" / name / "gens.json").write_text(json.dumps({"gens": metas}))
        # the frozen graph IS the version shown: no "workflow changed" badge
        (out / "data" / name / "version.json").write_text(json.dumps({"version": metas[0].get("version")}))
    (out / ".nojekyll").write_text("")          # serve dirs/files starting with _ as is
    print(f"site: {out}  ({len(gens)} generations)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--projects", type=Path, default=ROOT / "projects")
    ap.add_argument("--out", type=Path, default=ROOT / "site")
    ap.add_argument("gens", nargs="+", help="<project>/<gen>, e.g. cassone-demo/g1")
    a = ap.parse_args()
    build(a.projects, a.out, a.gens)
