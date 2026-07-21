# `tests/perf` — speed, stability, reactivity

The rest of `tests/` is pure-Python and hermetic. This suite is the opposite: it
drives the **live app** — the real server and the real editor in a real browser —
because the failures it hunts do not exist anywhere else. A transpiler unit test
cannot tell you the editor froze on reload, or that the glow lied about which
node was running.

```bash
docker compose up -d                       # the app must be running
NOODLE_PERF=1 python -m pytest tests/perf -v
```

Without `NOODLE_PERF=1` every test skips, so `pytest tests/` stays fast and
hermetic.

| env | default | |
|---|---|---|
| `NOODLE_PERF` | *unset* | must be `1` to run anything here |
| `NOODLE_URL` | `http://localhost:8090` | |
| `NOODLE_CHROMIUM` | `/usr/bin/chromium` | the bundled playwright build mismatches the cache |
| `NOODLE_PERF_SLOW` | `1` | multiplies every time budget, for a slow box |

## The three questions

**Speed** (`test_backend_speed.py`) — a warm re-run must be served by the memo
cache, repeat runs must not scatter, and the boot endpoints must be cheap. The
reference profile on the lego brick is **~15s cold → ~0.4s warm**.

**Stability** (`test_ui_stability.py`) — open, switch, return, reload, re-run,
ten times over, each step under a hard timeout. A hang is a failure, not a slow
test, so nothing here waits indefinitely. Also: no leaked `EventSource`, no
unbounded heap growth, no native dialog blocking a reload.

**Reactivity** (`test_ui_reactivity.py`) — does the app keep *answering* while it
works? Main-thread round-trip during a run, frame times on the canvas, and the
cost of a single param edit. An app that is merely slow tells you it is working;
one that stops answering looks broken.

**Truthfulness** (`test_progress_truth.py`) — the execution glow is the editor
making claims about its own work, and a lost event is a false claim, not a
cosmetic loss. The oracle is the run's own answer: `/execute` returns
`node_timings` and `node_cached` over a reliable HTTP response, so the
*unreliable* SSE stream is checked against them.

## Two things that make measurement here hard

**The worker is global and serialised.** One heavy run holds the lock and every
other client's run queues behind it, with no feedback at the requesting end.
Measured during development: a lego-brick execute that normally takes 0.4s took
**272 seconds** because another browser tab was mid-run on a heavier graph. So a
number from this suite is only meaningful when nothing else is using the app —
and `test_server_stays_responsive_during_a_run` exists to keep that fact visible
rather than mysterious.

**The projects are gitignored.** `BENCH_GRAPHS` in `conftest.py` names the graphs
the suite would like (small → heavy, so a regression shows as a shifted profile
rather than one number); any that are absent are skipped, never an error.
