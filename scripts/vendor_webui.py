"""Refresh pinned offline browser dependencies from the npm registry.

Run: python scripts/vendor_webui.py. No npm/node_modules or build step required.
Only runtime files and the transitive Three addon imports are copied.
"""
from pathlib import Path
import io
import re
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
DEST = ROOT / 'webui' / 'vendor'


def package(name, version):
    url = f'https://registry.npmjs.org/{name}/-/{name}-{version}.tgz'
    with urllib.request.urlopen(url, timeout=60) as response:
        archive = tarfile.open(fileobj=io.BytesIO(response.read()), mode='r:gz')
    return archive


def copy(archive, source, dest):
    data = archive.extractfile('package/' + source).read()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return data.decode('utf-8')


def main():
    with package('three', '0.170.0') as archive:
        dest = DEST / 'three-0.170.0'
        copy(archive, 'LICENSE', dest / 'LICENSE')
        copy(archive, 'build/three.module.min.js', dest / 'build/three.module.min.js')
        sources = '\n'.join((ROOT / 'webui' / name).read_text() for name in ['viewer.js', 'nodes.html', 'view.html', 'index.html'])
        todo = ['examples/jsm/' + x for x in re.findall(r"three/addons/([^'\"]+\.js)", sources)]
        seen = set()
        while todo:
            path = todo.pop()
            if path in seen:
                continue
            seen.add(path)
            text = copy(archive, path, dest / path)
            for rel in re.findall(r"(?:from\s*|import\s*)['\"](\.[^'\"]+)['\"]", text):
                child = (dest / path).parent / rel
                todo.append(str(child.resolve().relative_to(dest.resolve())))
    with package('litegraph.js', '0.7.18') as archive:
        for file in ['build/litegraph.min.js', 'css/litegraph.css', 'LICENSE']:
            copy(archive, file, DEST / 'litegraph-0.7.18' / file)
    with package('ace-builds', '1.36.2') as archive:
        for file in ['ace.js', 'mode-python.js', 'theme-one_dark.js']:
            copy(archive, 'src-min-noconflict/' + file, DEST / 'ace-1.36.2' / file)
        copy(archive, 'LICENSE', DEST / 'ace-1.36.2' / 'LICENSE')


if __name__ == '__main__':
    main()
