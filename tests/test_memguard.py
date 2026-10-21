"""The memory ceiling on engine runs (cad_nodes/memguard.py): a run past it
is killed instead of taking the machine down."""
import os

from cad_nodes import memguard


def test_it_measures_a_live_process_and_nothing_for_a_dead_one():
    assert memguard.used_mb(os.getpid()) > 0
    assert memguard.used_mb(2 ** 22 + 12345) is None
    assert memguard.over(os.getpid(), limit=0) and not memguard.over(os.getpid(), limit=10 ** 9)
    assert not memguard.over(None)


def test_the_ceiling_is_half_the_ram_unless_set(monkeypatch):
    monkeypatch.delenv("NOODLE_WORKER_MAX_MB", raising=False)
    assert memguard.limit_mb() >= 1536
    monkeypatch.setenv("NOODLE_WORKER_MAX_MB", "2048")
    assert memguard.limit_mb() == 2048
    assert "2.0 GB" in memguard.message()


def test_every_wait_on_a_worker_checks_it():
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "cad_nodes" / "executor.py").read_text()
    assert src.count("memguard.over(proc.pid, cap)") == 2      # warm and cold
