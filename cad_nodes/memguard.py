"""A ceiling on the memory of an engine run.

Paid for twice: a run that ran away in memory (a 🔦 replay on creepyfinger,
2026-10-21, and once before on the gpunix) filled RAM and swap, and the whole
machine stopped answering — screen included — until the power was pulled.
The container had no memory limit, and nothing in noodle watched.

The executor already polls every 0.1 s while it waits for a worker (warm or
cold); there it asks `over(pid)`, and a worker past the ceiling is killed: the
run fails with a message, the warm worker respawns, the machine stays up.
RAM + swap are counted together, or a process swapping out would look small
while it drowns the disk.

Ceiling: NOODLE_WORKER_MAX_MB, else half the machine's RAM (never under 1.5 GB).
Inside a container /proc/meminfo is the host's, which is the right number:
what is at stake is the host.
"""
from __future__ import annotations

import os

_FLOOR_MB = 1536


def limit_mb() -> int:
    env = os.environ.get("NOODLE_WORKER_MAX_MB", "").strip()
    if env:
        try:
            return max(256, int(float(env)))
        except ValueError:
            pass
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return max(_FLOOR_MB, int(line.split()[1]) // 1024 // 2)
    except OSError:
        pass
    return 4096


def used_mb(pid: int) -> int | None:
    """RAM + swap of one process, in MB (None if it is gone / unreadable)."""
    try:
        kb = 0
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith(("VmRSS:", "VmSwap:")):
                    kb += int(line.split()[1])
        return kb // 1024
    except (OSError, ValueError, IndexError):
        return None


def over(pid: int | None, limit: int | None = None) -> bool:
    if not pid:
        return False
    u = used_mb(pid)
    return u is not None and u > (limit if limit is not None else limit_mb())


def message() -> str:
    return (f"Memoria esaurita: la corsa ha superato {limit_mb() / 1024:.1f} GB ed è stata "
            "fermata prima di bloccare la macchina (NOODLE_WORKER_MAX_MB per cambiare il tetto).")
