"""Regression tests for job ownership and worker scheduling, without build123d."""
import concurrent.futures
import json
import threading
import time
from pathlib import Path

import pytest

from cad_nodes import executor as ex
from cad_nodes.job_files import atomic_write, progress_file, run_dir


def test_concurrent_runs_keep_scripts_results_and_progress_separate(tmp_path, monkeypatch):
    a_started, b_done = threading.Event(), threading.Event()

    def build(code, stl, view, quality, write_stl, progress_path, graph_key=None):
        return json.dumps({'code': code, 'view': str(view)})

    def run(script, cwd, timeout, cancel):
        assert cwd == tmp_path
        payload = json.loads(script.read_text())
        if payload['code'] == 'A':
            a_started.set()
            assert b_done.wait(3)
        Path(payload['view']).write_text(json.dumps({
            'success': True, 'counts': {'solids': 1}, 'owner': payload['code']}))
        return {'stdout': payload['code']}

    monkeypatch.setattr(ex, 'build_script', build)
    monkeypatch.setattr(ex, '_run_script', run)
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        a = pool.submit(ex.execute_code, 'A', tmp_path, run_id='A')
        assert a_started.wait(3)
        try:
            b = pool.submit(ex.execute_code, 'B', tmp_path, run_id='B').result(3)
        finally:
            b_done.set()
        a = a.result(3)
    assert a['stdout'] == a['view']['owner'] == 'A'
    assert b['stdout'] == b['view']['owner'] == 'B'
    assert json.loads((tmp_path / 'view.json').read_text())['owner'] == 'B'
    for tag in ['A', 'B']:
        lines = [json.loads(line) for line in progress_file(tmp_path, tag).read_text().splitlines()]
        assert lines[0]['r'] == tag
        assert lines[-1] == {'k': 'done', 'r': tag}
    with pytest.raises(FileExistsError):
        ex.execute_code('overwrite', tmp_path, run_id='A')


def test_opaque_run_ids_cannot_escape_project(tmp_path):
    assert run_dir(tmp_path, '../../escape').parent == tmp_path / '.runs'


def test_queue_wait_is_bounded_and_does_not_start_worker(tmp_path):
    worker = ex.WarmWorker()
    worker._lock.acquire()
    try:
        start = time.monotonic()
        res = worker.run(tmp_path / '_run.py', tmp_path, 0.05)
        assert res == {'timeout': True, 'phase': 'queue'}
        assert time.monotonic() - start < 1
        assert worker.status() == {'busy': False, 'queued': 0}
        assert not worker._alive()
    finally:
        worker._lock.release()


def test_queued_cancellation_does_not_kill_another_job(tmp_path):
    worker = ex.WarmWorker()
    worker._lock.acquire()
    cancel = tmp_path / 'cancel'
    cancel.touch()
    try:
        assert worker.run(tmp_path / '_run.py', tmp_path, 1, cancel) == {'cancelled': True}
        assert worker._lock.locked()
        assert worker.status()['queued'] == 0
    finally:
        worker._lock.release()


def test_active_cancellation_kills_only_the_owned_worker(tmp_path, monkeypatch):
    stub = tmp_path / 'worker.py'
    stub.write_text("import sys,time\nprint('@@CADWORKER@@{\"ready\":true}',flush=True)\n"
                    "for line in sys.stdin: time.sleep(10)\n")
    monkeypatch.setattr(ex, '_WORKER_PATH', str(stub))
    worker = ex.WarmWorker()
    cancel = tmp_path / 'cancel'
    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        future = pool.submit(worker.run, tmp_path / '_run.py', tmp_path, 5, cancel)
        deadline = time.monotonic() + 3
        while not worker._alive() and time.monotonic() < deadline:
            time.sleep(.01)
        # Wait for the stub to send ready, not cancel during startup.
        time.sleep(.1)
        cancel.touch()
        assert future.result(3) == {'cancelled': True}
    assert not worker._alive()
    assert worker.status() == {'busy': False, 'queued': 0}


def test_cold_run_cancellation(tmp_path, monkeypatch):
    monkeypatch.setattr(ex, '_warm_enabled', False)
    script = tmp_path / '_run.py'
    script.write_text('import time; time.sleep(10)')
    cancel = tmp_path / 'cancel'
    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        result = pool.submit(ex._run_script, script, tmp_path, 5, cancel)
        time.sleep(.1)
        cancel.touch()
        assert result.result(3) == {'cancelled': True}


def test_atomic_write_replaces_complete_file_and_leaves_no_temp_files(tmp_path):
    target = tmp_path / 'graph.json'
    atomic_write(target, 'old')
    atomic_write(target, b'new')
    assert target.read_text() == 'new'
    assert list(tmp_path.iterdir()) == [target]


def test_export_uses_warm_memo_and_returns_an_immutable_file(tmp_path, monkeypatch):
    def transpile(graph, memo=False):
        assert memo is True
        return '# cached graph'

    def run(script, cwd, timeout):
        assert cwd == tmp_path
        (script.parent / 'output.step').write_text('STEP')
        return {}

    monkeypatch.setattr(ex, 'transpile', transpile)
    monkeypatch.setattr(ex, '_run_script', run)
    a = ex.export_graph(None, tmp_path)
    b = ex.export_graph(None, tmp_path)
    assert a != b
    assert a.read_text() == b.read_text() == (tmp_path / 'output.step').read_text()


def test_cancelled_warm_failure_does_not_cold_run(tmp_path, monkeypatch):
    monkeypatch.setattr(ex, '_warm_enabled', True)

    def boom(*a, **k):
        raise RuntimeError('spawn failed')

    monkeypatch.setattr(ex._WORKER, 'run', boom)
    cancel = tmp_path / 'cancel'
    cancel.touch()
    assert ex._run_script(tmp_path / '_run.py', tmp_path, 1, cancel) == {'cancelled': True}


def test_generated_script_compiles_with_progress(tmp_path):
    script = ex.build_script('pass', tmp_path/'output.stl', tmp_path/'view.json',
                             progress_path=tmp_path/'progress.jsonl')
    compile(script, '_run.py', 'exec')
