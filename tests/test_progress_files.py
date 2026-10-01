"""Exercise the real SSE generator without importing the FastAPI deployment."""
import ast
import asyncio
import json
from pathlib import Path

from cad_nodes.job_files import atomic_copy, atomic_write, cleanup_runs, progress_file


def tailer():
    tree = ast.parse((Path(__file__).resolve().parents[1] / 'server.py').read_text())
    selected = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name in {'_tail_progress', '_run_id_of'}]
    ns = {'Path': Path, 'Request': object, 'asyncio': asyncio, 'json': json,
          'progress_file': progress_file, '_PROGRESS_MAX_IDLE': .5}
    exec(compile(ast.Module(body=selected, type_ignores=[]), 'server.py', 'exec'), ns)
    return ns['_tail_progress']


def progress(project, tag):
    path = progress_file(project, tag)
    atomic_write(path, json.dumps({'k': 'run', 'r': tag}) + '\n' +
                 json.dumps({'k': 's', 'n': tag}) + '\n' +
                 json.dumps({'k': 'done', 'r': tag}) + '\n')
    return path


def test_late_subscriber_reads_its_run_even_after_another_run_started(tmp_path):
    own = progress(tmp_path, 'own')
    progress(tmp_path, 'new')
    atomic_write(tmp_path / 'progress.jsonl', '{"k":"run","r":"new"}\n')

    async def collect():
        return [event async for event in tailer()(own, 'own')]

    events = asyncio.run(collect())
    assert len(events) == 2
    assert all('own' in event for event in events)


def test_legacy_subscriber_ignores_old_events_and_waits_for_next_run(tmp_path):
    progress(tmp_path, 'old')
    pointer = tmp_path / 'progress.jsonl'
    atomic_write(pointer, '{"k":"run","r":"old"}\n')

    async def collect():
        async def start():
            await asyncio.sleep(.06)
            progress(tmp_path, 'new')
            atomic_write(pointer, '{"k":"run","r":"new"}\n')
        writer = asyncio.create_task(start())
        events = [event async for event in tailer()(pointer)]
        await writer
        return events

    events = asyncio.run(collect())
    assert len(events) == 2
    assert all('new' in event for event in events)


def test_cleanup_only_removes_old_completed_jobs(tmp_path):
    import os
    import time
    active = tmp_path / '.runs' / 'active'
    active.mkdir(parents=True)
    for name in ['old', 'new']:
        job = tmp_path / '.runs' / name
        job.mkdir()
        (job / 'complete').touch()
    os.utime(tmp_path / '.runs/old/complete', (0, time.time() - 1000))
    cleanup_runs(tmp_path, keep=1, grace=180)
    assert active.exists()
    assert (tmp_path / '.runs/new').exists()
    assert not (tmp_path / '.runs/old').exists()


def test_atomic_copy_keeps_the_source_immutable(tmp_path):
    source, target = tmp_path / 'source', tmp_path / 'target'
    source.write_bytes(b'new')
    target.write_bytes(b'old')
    atomic_copy(source, target)
    assert target.read_bytes() == b'new'
    target.write_bytes(b'changed')
    assert source.read_bytes() == b'new'  # not a hardlink
