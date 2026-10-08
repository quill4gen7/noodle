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
                               + <gen>/notes.json, notes/<images>, tags.json, measures.json when present

A generation can be given as  <project>/<gN>[=<shown-name>][:<note ids>]
    zz-note-probe/g2=bolt-and-nut-notes:a1,a21,a23
publishes the gen under another name (a scratch project should not be the
public URL) and only the notes listed (none listed = all of them). Notes are
what the user DREW on a generation (✎ Disegna) and the agent's tags on it: the
preview shows them, and the pen works — only sending a note needs a server,
so on Pages "Invia all'agente" explains that and links the install.

--base-site DIR carries over the data of demos that are not being rebuilt
(their frozen gen may exist nowhere else any more): every data/<name>/ in DIR
that this build does not produce is copied as is.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEBUI = ROOT / "webui"
# What to paste to your own agent to get noodle installed. ONE file: the build
# puts it on the landing page and in the preview's "send" message, and the
# README quotes it verbatim (tests/test_pages.py keeps the copy honest).
INSTALL_PROMPT = (ROOT / "scripts" / "pages" / "install-prompt.txt").read_text().strip()

# Runs before the page's module: the read-only viewer asks the API for a few
# things; on Pages they are files next to the demo. Writes have nowhere to go:
# the ones the page makes on its own (seen, card picture, error report) are
# answered OK and dropped; saving or deleting a note answers 501 STATIC_PREVIEW,
# which the patched page turns into "install noodle to send it to an agent".
FETCH_SHIM = """<script>
(() => {
  const real = window.fetch.bind(window);
  const json = (o, status) => Promise.resolve(new Response(JSON.stringify(o),
    {status, headers: {'Content-Type': 'application/json'}}));
  const map = url => {
    let m = /^\\/api\\/graph\\/([^/]+)\\/gens\\/(g\\d+)\\/(view|graph|meta|notes|tags|measures)$/.exec(url);
    if (m) return `../data/${m[1]}/${m[2]}/${m[3]}.json`;
    m = /^\\/api\\/graph\\/([^/]+)\\/(gens|version)$/.exec(url);
    if (m) return `../data/${m[1]}/${m[2]}.json`;
    m = /^\\/api\\/gens\\/recent\\?.*project=([^&]+)/.exec(url);
    if (m) return `../data/${m[1]}/gens.json`;
    return url;
  };
  window.fetch = (input, init) => {
    const method = ((init && init.method) || 'GET').toUpperCase();
    if (typeof input === 'string' && method !== 'GET') {
      if (/\\/notes(\\/a\\d+)?$/.test(input)) return json({detail: 'STATIC_PREVIEW'}, 501);
      return json({ok: true, static: true}, 200);
    }
    return real(typeof input === 'string' ? map(input) : input, init);
  };
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
    # a picture placed on a saved note is loaded by an <img>, not fetch: no shim
    sub("`/api/graph/${encodeURIComponent(NAME)}/gens/${GEN}/notes/${n.id}/img/${im.image}`",
        "`../data/${encodeURIComponent(NAME)}/${GEN}/notes/${im.file}`")
    # ✎ Disegna works here — strokes, text, pictures, eraser, undo — but a note
    # is sent to an agent through a running noodle: say so, keep the drawing
    sub("    toast(`⚠ Nota non salvata: ${esc(err.message)}`);",
        "    if (err.message === 'STATIC_PREVIEW') toast(STATIC_SEND, 30000);\n"
        "    else toast(`⚠ Nota non salvata: ${esc(err.message)}`);")
    sub("function toast(html, ms = 6000) {",
        "const INSTALL_PROMPT = " + json.dumps(INSTALL_PROMPT) + ";\n"
        "window.__copyInstall = async b => {\n"
        "  try { await navigator.clipboard.writeText(INSTALL_PROMPT); b.textContent = '✓ Copiato: incollalo al tuo agente'; }\n"
        "  catch { prompt('Copia questo e dallo al tuo agente:', INSTALL_PROMPT); }\n"
        "};\n"
        "const STATIC_SEND = '✎ Il disegno funziona, ma questa è l\\'<b>anteprima statica</b>: qui non c\\'è un agente '\n"
        "  + 'a cui mandarlo. Per mandarlo davvero, <b>fai installare noodle al tuo agente</b> (Claude Code, Codex, Cursor…): '\n"
        "  + 'controlla il repo, lo installa con Docker e si collega da solo.<br>'\n"
        "  + '<button class=\"btn\" style=\"margin:6px 0\" onclick=\"__copyInstall(this)\">📋 Copia il prompt di installazione</button> '\n"
        "  + '<a href=\"../#install-agent\">vedi il prompt</a> · '\n"
        "  + '<a href=\"https://github.com/rederyk/noodle#install--run\" target=\"_blank\" rel=\"noopener\">a mano</a><br>'\n"
        "  + 'Il disegno resta qui.';\n"
        "function toast(html, ms = 6000) {")
    return html.replace("<head>", "<head>\n" + FETCH_SHIM, 1)


def _parse(spec: str) -> tuple[str, str, str, list[str] | None]:
    """<project>/<gN>[=<shown-name>][:<note ids>] → (project, gen, shown, ids)."""
    ref, _, ids = spec.partition(":")
    ref, _, shown = ref.partition("=")
    name, gen = ref.split("/")
    return name, gen, shown or name, [i for i in ids.split(",") if i] or None


def _copy_notes(src: Path, dst: Path, shown: str, ids: list[str] | None) -> int:
    """The gen's notes (only `ids`, if given) as one notes.json, their placed
    pictures next to it, and the agent's tags.json and measures.json. Returns notes copied."""
    notes_dir, out = src / "notes", []
    if notes_dir.is_dir():
        for f in sorted(notes_dir.glob("a*.json"), key=lambda f: int(re.sub(r"\D", "", f.stem) or 0)):
            if not re.fullmatch(r"a\d+", f.stem) or (ids and f.stem not in ids):
                continue
            note = json.loads(f.read_text())
            note["graph"] = shown
            for im in note.get("images") or []:          # {image: k, file: aK.imgK.png}
                f_img = im.get("file")
                if f_img and (notes_dir / f_img).exists():
                    (dst / "notes").mkdir(exist_ok=True)
                    shutil.copy(notes_dir / f_img, dst / "notes" / f_img)
            out.append(note)
    if out:
        (dst / "notes.json").write_text(json.dumps({"notes": out}))
    if (src / "tags.json").exists():
        shutil.copy(src / "tags.json", dst / "tags.json")
    if (src / "measures.json").exists():
        shutil.copy(src / "measures.json", dst / "measures.json")
    return len(out)


def build(projects: Path, out: Path, gens: list[str], base_site: Path | None = None) -> None:
    if out.exists():
        shutil.rmtree(out)
    (out / "demo").mkdir(parents=True)
    (out / "static").mkdir()
    (out / "demo" / "index.html").write_text(_patch_view((WEBUI / "view.html").read_text()))
    viewer = (WEBUI / "viewer.js").read_text()
    viewer = re.sub(r"(['\"])/static/", r"\1../static/", viewer)
    (out / "static" / "viewer.js").write_text(viewer)
    # the ↔ Metro's snaps: measuring is computation in the browser, so it works here too
    shutil.copy(WEBUI / "measure.js", out / "static" / "measure.js")
    shutil.copy(WEBUI / "icon.svg", out / "static" / "icon.svg")
    shutil.copytree(WEBUI / "vendor" / "three-0.170.0", out / "static" / "vendor" / "three-0.170.0")
    # landing page + media
    pages = ROOT / "scripts" / "pages"
    if (pages / "index.html").exists():
        import html as _html
        (out / "index.html").write_text((pages / "index.html").read_text()
                                        .replace("{{INSTALL_PROMPT}}", _html.escape(INSTALL_PROMPT)))
    if (pages / "thumbs").is_dir():
        shutil.copytree(pages / "thumbs", out / "thumbs")
    shutil.copytree(ROOT / "docs" / "asset", out / "assets")
    shutil.copy(WEBUI / "logo.svg", out / "assets" / "logo.svg")
    by_project: dict[str, list[dict]] = {}
    for spec in gens:
        name, gen, shown, ids = _parse(spec)
        src = projects / name / "gens" / gen
        dst = out / "data" / shown / gen
        dst.mkdir(parents=True)
        for part in ("view", "graph"):
            shutil.copy(src / f"{part}.json", dst / f"{part}.json")
        meta = json.loads((src / "meta.json").read_text())
        meta["graph"] = shown
        (dst / "meta.json").write_text(json.dumps(meta))
        n = _copy_notes(src, dst, shown, ids)
        if n:
            print(f"  {shown}/{gen}: {n} note(s)")
        by_project.setdefault(shown, []).append(meta)
    for name, metas in by_project.items():
        metas.sort(key=lambda m: int(m["gen"][1:]), reverse=True)
        for m in metas:              # nothing to upload a card picture to
            m["thumb"] = True
        (out / "data" / name / "gens.json").write_text(json.dumps({"gens": metas}))
        # the frozen graph IS the version shown: no "workflow changed" badge
        (out / "data" / name / "version.json").write_text(json.dumps({"version": metas[0].get("version")}))
    kept = []
    if base_site and (base_site / "data").is_dir():
        for d in sorted((base_site / "data").iterdir()):
            if d.is_dir() and not (out / "data" / d.name).exists():
                shutil.copytree(d, out / "data" / d.name)
                kept.append(d.name)
    # every gen answers its notes/tags reads, if only with nothing: a 404 is
    # handled by the page, but it fills the console of everyone who looks
    for d in (out / "data").glob("*/g*"):
        if not d.is_dir():                       # gens.json starts with a g too
            continue
        if not (d / "notes.json").exists():
            (d / "notes.json").write_text('{"notes": []}')
        if not (d / "tags.json").exists():
            (d / "tags.json").write_text('{"tags": []}')
        if not (d / "measures.json").exists():
            (d / "measures.json").write_text('{"measures": []}')
    (out / ".nojekyll").write_text("")          # serve dirs/files starting with _ as is
    print(f"site: {out}  ({len(gens)} generations built"
          + (f", carried over: {' '.join(kept)})" if kept else ")"))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--projects", type=Path, default=ROOT / "projects")
    ap.add_argument("--out", type=Path, default=ROOT / "site")
    ap.add_argument("--base-site", type=Path, default=None,
                    help="a previously built site whose data/<name>/ are carried over when not rebuilt")
    ap.add_argument("gens", nargs="+", help="<project>/<gN>[=<shown-name>][:<note ids>], e.g. cassone-demo/g1")
    a = ap.parse_args()
    build(a.projects, a.out, a.gens, a.base_site)
