# Threads — what noodle builds, and why it builds it out of triangles

One node, `Thread` (category `fastener`), makes a real screw thread: ISO metric,
trapezoidal lead screws, UNC/UNF, ACME, and tapered NPT pipe — male or female,
right or left handed, single or multi-start, with the clearance a printer needs.

This document is the record of what was measured on the way, because two of the
decisions look arbitrary until you see the numbers.

---

## 1. build123d has no threads, and OCCT cannot be talked into them

build123d 0.11 ships no thread primitive. `IsoThread`, `TrapezoidalThread`,
`AcmeThread` and friends live in **`bd_warehouse`**, which is not a dependency of
this image. So the geometry is ours to build, and the first question is which
lane it belongs on.

A thread is the classic helical sweep: an axial section, one pitch tall, swept
along a helix so consecutive turns abut at ±p/2 and fuse into a continuous ridge.
Then the ridge is unioned to a core cylinder. Measured on a real ISO 68-1
profile, with build123d's `sweep(..., is_frenet=True)`:

| thread | sweep | union with the core | result |
|---|---|---|---|
| M6×1, L=12 | 0.03 s | **6.52 s** | `is_valid=False`, volume **227.9** — *the bare core*. The thread is gone. |
| M10×1.5, L=20 | 0.04 s | **8.28 s** | `is_valid=False` |
| M20×2.5, L=30 | 0.04 s | **2.17 s** | `is_valid=True`, volume **0** |

The sweep itself is fine and fast. **It is the boolean that destroys the part**,
slowly, and — this is the dangerous bit — *without raising*. A user would get a
plain cylinder back and no indication that anything failed.

The obvious fix (let the section abut the core exactly instead of overlapping it,
so there are no interfering solids) does not build at all:

```
StdFail_NotDone: BRep_API: command not done
```

Both variants were swept across sizes. There is no setting that rescues this.

**So threads live on the mesh lane** — the same conclusion, for the same kind of
reason, as everything in `PLAN_MESH_LANE.md`. The identical construction through
trimesh + manifold3d:

| thread | total | watertight | major Ø (nominal) |
|---|---|---|---|
| M6×1, L=12 | 0.081 s | yes | 6.000 (6) |
| M12×1.75 | 0.059 s | yes | 11.998 (12) |
| Tr20×4 | 0.022 s | yes | 20.000 (20) |
| 1/4-20 UNC | 0.071 s | yes | 6.350 (6.35) |
| 1/2-10 ACME | 0.048 s | yes | 12.699 (12.7) |

Roughly **100× faster, and correct**. For a part whose whole purpose is to be
printed, a mesh is the native output anyway.

---

## 2. Four families, one trapezoid

Every thread form here is the same section with different numbers: a crest flat,
two flanks at a half-angle, a root flat. One builder covers all of them.

| family | half angle | crest flat | depth | taper |
|---|---|---|---|---|
| ISO metric / UNC / UNF | 30° | p/8 | 17H/24 ext, 15H/24 int | — |
| Trapezoidal (Tr) | 15° | 0.366 p | 0.5 p + a_c | — |
| ACME | 14.5° | 0.3707 p | 0.5 p + 0.254 mm | — |
| NPT | 30° | 0.0381 p | 0.8 p | 1:16 on Ø |

`H = (√3/2)·p` is the sharp-V height. The guard that matters is `2·hw < p` — the
flanks must reach the root before they reach each other — and it is swept over
every size in the dropdown by `tests/test_thread.py`, which is how a mistyped
pitch in a table nobody rereads gets caught.

Inch families are stored the way they are *quoted* (nominal diameter in inches,
threads per inch) and converted once in `_unified_sizes`, rather than
hand-transcribed into a millimetre table that cannot be proofread.

## 3. Male and female are the same thread, truncated differently

This is worth stating plainly because it collapses what looks like two problems
into one. On the 60° families the external and internal threads differ **only in
how deep the root is cut**:

- external `17H/24` → ISO minor `d3 = d − 1.2269 p`
- internal `15H/24` → ISO basic `D1 = d − 1.0825 p`

And the female thread is never modelled directly. The `internal` result **is the
tap**: the male thread grown by the clearance. Subtract it from a body and it
drills the hole and cuts the thread in one operation, exactly as a real tap does.

## 4. Clearance — the only number that matters on a printer

Verified the one way that means anything: generate a male thread, generate its
nut, and measure the **boolean interference** of one inside the other.

```
M6×1, L=10, interference (mm³)
segments   clear=0.10   clear=0.20   clear=0.30
      32      0.00094      0.00001     -0.00000
      64      0.01334      0.00198      0.00000
     128      0.00022      0.00032      0.00027
     192     -0.00020      0.00083      0.00077
```

Read this carefully: the residuals are **not monotone in the facet count and go
slightly negative**, which means they are manifold3d's numerical noise on a
near-empty intersection, not real contact. The largest is 0.013 mm³ against a
thread of ~225 mm³ — 0.006%.

The honest conclusion: at clearance **0** the two basic profiles are tangent by
construction and will bind; from **0.1 mm** upward the pair is free. The default
is **0.3 mm** because on an FDM printer the machine's own error dwarfs the
model's, not because the geometry needs it.

**`clearance` loosens the thread it is set on** — the male shrinks, the female
grows. Set it on *one* half of a mating pair. Setting it on both doubles the gap;
that is a sloppy fit, not a failure, and the node description says so.

## 5. Three traps, paid for

- **An inverted winding is silent.** manifold3d reads a reversed triangle order as
  **negative volume** and *subtracts* the rib instead of adding it. The first
  working version returned a M6 rod of 180.5 mm³ against a bare core of 227.9 —
  smaller than its own core, still watertight, no error anywhere. The faces are
  built inward-facing and reversed once, deliberately, with a comment.
- **The section must reach INTO the core** (`base = root − 0.15p`). Tangency is
  what made OCCT fail outright, and manifold3d wants the overlap too.
- **A `shape` fatter than the root swallows the thread.** An external thread
  brings its own core, so `shape` is for what you thread *onto* — a head, a
  flange, a boss. The first version of `bolt-and-nut.json` wired an Ø8 shank into
  an M8 thread, whose major diameter is also 8: the union filled every groove and
  produced a plain cylinder. No error, correct volume, and it takes a rendered
  picture to notice. The example is now one node — the thread, with the head as
  its `shape` — which is also the better demonstration.
- **The placement socket is `at`, not `origin`.** The transpiler wraps an `origin`
  socket around a node's *whole result* (`_at` is applied outside the template),
  which with `shape` wired would displace the finished assembly — so a tapped hole
  could never be put anywhere but the axis. Taking the point inside the helper
  places the thread *before* the boolean, which is the only useful reading. It
  also means a **list** of points drills a whole pattern of tapped holes in one
  node, for free, via `_at`'s existing many-origins branch.

## 6. What is not built

- **No thread relief / runout.** The thread starts and stops flat (with an
  optional 45° lead-in chamfer, flared on the female so the mouth guides a bolt).
  Real fasteners have an undercut at the shoulder.
- **No thread classes or tolerance grades** (6g/6H, 2A/2B). `clearance` is a
  single number, which is the right abstraction for a printer and the wrong one
  for a machine shop.
- **NPT is the taper, not the standard.** The cone and the profile are right; the
  engagement lengths, the L1/L3 gauge planes and anything about actually sealing
  are not modelled. Do not use it for a pressure fitting without checking it.
- **No bolt/nut/washer library.** `Thread` makes threads; a hex head is a
  `Polyhedron` away. `cad_nodes/examples/bolt-and-nut.json` shows the assembly.
- **No heat-set insert pockets** — a natural next node, and a different shape
  (stepped bore, no thread).

Tests: `tests/test_thread.py` (pure-Python: tables, profile arithmetic, wiring
contract). The geometry runs in the worker and is measured there.
