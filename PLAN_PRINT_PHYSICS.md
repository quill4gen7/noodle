# PLAN — print physics

Four nodes that answer the only question a slicer never asks you: **which way up, and
why**. They live in the catalog category `print` (`catalog.py` §12c), they run on the
mesh lane (`PLAN_MESH_LANE.md`), and the runtime is in the transpiler PREAMBLE.

---

## 1. The physics, in one paragraph

A printed part is **anisotropic**. Within a layer the plastic is continuous; between two
layers it is *glued*, and that bond is worth roughly a third to two thirds of the real
material (it depends on polymer, nozzle and chamber temperature, layer height and line
width — treat the ratio as a fact of life, not as a number to quote). So a part fails at
a **layer line**, and the layer line it fails at is the one with the least glued area.

Two consequences, and every node here is one of them:

- **Where it breaks** is the smallest cross-section perpendicular to Z. Call it the *weak
  plane*. It is not a property of the part — it is a property of the part **in this
  orientation**, and turning the part moves it.
- **Which way to turn it** cannot be answered without knowing what the part is *for*.
  Given the direction the part will be pulled, the rule is: keep that load **in** the
  layer plane, never across it.

These are heuristics, not an FEA. They catch the dominant failure mode and say nothing
about a stress riser around a hole. Use them to rule out the stupid orientation, not to
certify the clever one.

## 2. The nodes

| node | what it does |
|---|---|
| `PlaceOnBed` | lowest point → z=0, optionally centred in XY. Serves **both lanes** (measures on the mesh, moves the original), so a solid stays a solid |
| `Drop` | PlaceOnBed as a **fall you can scrub**: a `timeline` slider from 0 (where the part is) to 1 (at rest), analytic bounce under gravity, restitution fixed per `material` (plastic 0.55, rubber 0.85, steel 0.65, wood 0.45, lead 0.08, clay 0). Optional `plane` input sets what it falls onto (default the bed). Both lanes, translation only |
| `ContainerMotion` | a **prescribed** motion for a Drop's `container` — one translation + one rotation, `cycles` choosing ramp (tilt, pour) or oscillation (shake, stir). Not gravity: you dictate it, and the contents answer through contact alone (§2c) |
| `PrintCheck` | text report → a Panel: height and layers, bed contact, overhang area past the critical angle, and the **weak plane** (area + height) |
| `OverhangFaces` | the faces that need support, as a mesh of its own — so the viewer gives them their own colour and you *see* them on the part |
| `SupportVolume` | the support material itself, **as a body**: preview it, inspect it, export it. The honest cost, not a gesture at it |
| `OrientForPrint` | tries every stable resting pose, scores it, returns the winner already on the bed + a `report` table of the top five |

`OrientForPrint` has **two outputs** from one search (`_emit_orient`, modelled on
`_emit_center`): scoring the poses means slicing each of them, and doing it twice because
a Panel happens to be wired in would be daft.

`Drop` (`_drop` + `_drop_segs`/`_drop_height` + `_settle_plan` in the PREAMBLE) is two
phases on one slider. **Bounce**: one free fall plus a geometric series of parabolas —
each impact keeps `e` of the speed, bounces below 0.1% of the drop count as rest. With
`e=0.08` (lead) the fall is nearly the whole phase — one dead thud — where rubber spends
most of it bouncing. **Topple** (`settle`, on by default): the quasi-static cascade. A
resting body is stable iff its centre of mass projects inside the support polygon — the
same test OrientForPrint uses to *enumerate* stable poses; `_settle_plan` walks the *path*
between them on the convex hull: com outside the contact patch → tip about the nearest
support edge (or corner) until the next hull facet touches, repeat (≤40 steps). Each step
is recorded as (pivot, axis, angle, seconds) and replayed — partially, ease-in `f²` — at
scrub time, composed as `Shape.rotate(Axis(...))` on the B-Rep lane and one 4×4 on the
mesh lane, conjugated through the wired plane's frame.

Two rules carry the honesty. The **energy guard**: a step that does not strictly lower the
centre of mass is not a topple — a tessellated sphere "toppling" facet to facet releases
nothing and is declared at rest (it may creep a facet or two first — physically fair),
where a cube balanced on an edge drops its centre 20% and goes over. And **deterministic
ties**: a part balanced exactly (the 45° cube — com dead over the support edge, zero
torque) tries both senses and takes the one that descends; a graph must give the same
answer twice, where a real part would be tipped by the first draught.

The timeline is bounce seconds + topple seconds (g = 9810 mm/s²), normalised to the
slider: `t=1` is always fully at rest, material and topples change the *shape* of the
journey, not its reach. Heights are measured on the tessellation (same reason as
`PlaceOnBed`); a part starting *under* the plane surfaces linearly — it cannot fall. The
com is `trimesh.center_mass` when the mesh is watertight, the bbox centre otherwise.
Why this took a second pass: the bounce has a closed form, toppling does not — it is
contact dynamics (which edge, how far, then which edge next), a genuinely simulated
cascade, just quasi-static instead of a full rigid-body integrator.

And because the plan is *data* — segments and steps, not code — the engine ships it with
the preview (`_noodle_anim` on the result → `previews[id].anim` in view.json) and the
editor replays any t in the browser as pure matrix math (`dropMatrixAt`, nodes.html):
with Live on, dragging the t slider (or a Number Slider wired into `t`, or the
timeline gizmo) animates the fall at 60fps with zero engine round trips; the exact
re-bake lands when the drag settles. One physics, computed once, played anywhere.

**Collisions (`collide`, off by default).** Several shapes wired into ONE Drop become one
scene instead of a fan (the transpiler un-fans `shape`; the output is a list again). This
is where the quasi-static single-body model gives way to **real rigid-body dynamics**
(`_dyn_sim`, pybullet in DIRECT mode): every part is its convex hull, and they all fall
**together** — colliding in mid-air, pushing each other over, tumbling, stacking — because
coupled contact is exactly what a sequential lowest-first cascade cannot express. The run
is simulated once at a fixed 1/240 s step (deterministic for a given scene on a given
build) and recorded as 60 Hz keyframes per body until the scene sleeps.

Three engine choices carry the quality. **Units are millimetres directly, not scaled to
metres**: pybullet's collision margin is a fixed absolute value, so a 20 mm cube shrunk to
0.02 m rests ~1 mm above the bed, where the same cube at mm scale rests sub-micron —
verified across scales (S=1 → 0.001 mm gap, S=0.001 → 1 mm). Tunnelling risk is
scale-invariant (speed × dt / thickness), so nothing is lost, and a per-body swept sphere
(CCD) covers the thin, fast cases. **`restitutionVelocityThreshold` = 100 mm/s**: below it
a contact is inelastic — the 1 mm/s I first used made every real contact elastic and the
pile jittered for the full 8 s instead of settling; with the threshold plus rolling /
spinning friction and angular damping a three-box scene sleeps in ~0.6-1 s. **Mass is the
hull volume**, so ratios are physical (a heavy part settles a light one, not the reverse).

Because the plan is again *data* — per-body keyframe tracks (times, positions,
quaternions) — the whole scene replays in the browser: each body carries its own
`_noodle_anim` of kind `"keys"`, `mesh_extractor` emits a `{kind:"Scene", bodies:[…]}`
preview, `viewer.js` builds a Group of independently-posable meshes, and `nodes.html`
(`keyInterp` + `sceneBodyPose`, lerp + slerp) moves each body to any t while the slider
drags — the same live-replay story as the single drop, now for a whole pile. The demo
(`drop-stack`): three boxes fall as one scene — one lands, one stacks, and the half-off
cube tumbles over the edge, rolls off and ends flat on the bed beside. Declared limits:
the falling parts are convex HULLS, not their true meshes; rest poses carry the
solver's contact margin (sub-micron in mm units), not CAD exactness; deterministic per
scene but chaotic in the physical sense — nudge a part a hair and the pile lands
differently. That is not a bug; that is what falling is.

### 2b. The container — the one body that keeps its concavity

Hulls are fine for the things that fall and fatal for the thing they fall *into*: the hull
of a bowl is a dome, and a dome sheds. So `Drop` grew a second input, **`container`** — an
immovable collider that never moves and is never an output.

Bullet will accept a **concave triangle soup** for a body, but only a static one
(`GEOM_FORCE_CONCAVE_TRIMESH`, mass 0). That restriction lines up exactly with what a
container is, so the trade writes itself: **the thing that holds is exact, the things that
fall are hulls.** `_static_colliders` turns the wired shape (or shapes — several may be
wired) into (vertices, faces) in bed coordinates and `_dyn_sim` registers each as a static
body, a little grippier and deader than the bed (restitution 0.2, lateral friction 0.9,
plus rolling/spinning friction) so a part that lands in a bowl *stops* there instead of
skating round the cavity for the full 8 s.

Verified against the analytic seat: a hemispherical bowl of inner radius 18 (floor at
z = 2), three balls of r = 4 poured in. They come to rest at radial offset 1.63 / 6.19 /
8.94 and heights 6.17 / 7.56 / 9.24 mm — where a ball resting on that inner wall at radius
ρ must sit at 20 − √(14² − ρ²) = 6.00 / 7.44 / 9.22. The first sits on the floor, the other
two nestle against it and ride up the wall, each within 0.03 mm of where the geometry says
it must be.

A wired container **implies scene mode**, whatever the `collide` toggle says: the emitter
un-fans the shapes on `collide or container`, because parts falling into one bowl are one
scene by definition, and a single part dropped into a bowl is the whole point of the
socket. One consequence reaches the browser: a single body now arrives as a plain preview
carrying a keyframe plan rather than a `Scene`, so `applyDropAnim` routes an anim of kind
`"keys"` to `sceneBodyPose` instead of the analytic `dropMatrixAt`.

Open: a very heavy container mesh is fed to bullet whole (it builds a BVH — fine so far,
but a 200k-triangle bowl has not been measured). "You cannot shake the bowl" was the other
one, and §2c closes it.

### 2c. Moving the thing that was holding still (`ContainerMotion`)

A container that only ever holds is half a container. `ContainerMotion` → the `motion`
socket makes it tilt, shake, spin or tip — and the point is that this is **dictated, not
simulated**. Gravity is not doing it; you are. The parts inside answer only through
contact and friction, which is exactly why they lag behind a spin, slide, climb the wall
and spill rather than following the container rigidly.

One generic node instead of a menu of presets, because **`cycles` is the real axis**:

| | `cycles` | what it is |
|---|---|---|
| tilt / pour / tip a crate | 0 | a **ramp** — go there once and stay |
| shake / stir / vibrate / tap | > 0 | an **oscillation** about the start pose, returning to it |

Everything else falls out: pour = rotate ~110–135° (past the wall, or nothing comes out),
cycles 0; shake = move 10, cycles 8; settle a powder = move z 3, cycles 20; centrifuge =
rotate z 720. `delay` waits before it starts — fill the bowl, *then* tilt it. Rotation is
about the container's own centre unless a `pivot` is wired (the hinge of a hopper, the lip
a crate goes over).

**How to drive a static body — measured, because the obvious way silently does nothing.**
A tray translated 50 mm in 1 s under a resting box:

| driver | box carried |
|---|---|
| `resetBasePositionAndOrientation` alone | **−1.2 %** (nothing) |
| `reset…` + `resetBaseVelocity` every step | **99.3 %** |
| mass > 0 + `JOINT_FIXED` + `changeConstraint` | 99.9 % |

The first *teleports* the body: the contact exists but with zero relative velocity, so the
friction solver has nothing to transmit and the tray slides out from under the part. The
third works and is still wrong here — **mass > 0 forbids `GEOM_FORCE_CONCAVE_TRIMESH`**, so
it would hull the bowl and throw away the cavity, which is the only reason §2b exists. So:
mass 0, concave, reset pose *and* velocity every step. The velocity is a forward difference
of the prescribed pose, and its angular part is taken exactly (axis-angle) rather than by
the usual `2·dq.xyz/dt` small-angle reading, which drifts ~1 % by 30° a step — a spin node
is entitled to ask for a whole turn in a fraction of a second.

**Frames.** The user dictates the motion in world xyz; the colliders live in bed
coordinates. `_motion_driver` carries both over — `R_bed = Bᵀ R_world B` — and since bullet
poses a body as `x → R x + pos`, rotating about a pivot is *entirely* the `pos = p − R p`
term. Get that wrong and the container swings through the scene on an invisible arm
instead of turning in place; there is a test pinning the pivot fixed.

**A driven rig must not be allowed to fall asleep.** `_dyn_sim` exits after half a second
of calm, which for a tilt that begins at t = 1 s would fire *before the motion starts* and
freeze the whole pile mid-ride. Hence the `tau <= _drive_until` guard and `_t_max` grown to
cover the motion. A shaker never settles by construction, so it runs its full length.

**Drawing it took no frontend change.** A moving container has to be seen moving or the
pile looks haunted — but it is not an output and must not become one. So the posed
container rides the result as `_noodle_extra`, `mesh_extractor._preview_of` turns those
into extra bodies of the same `Scene` preview with their own `kind:"keys"` tracks, and
`viewer.js` + `sceneBodyPose` already know how to render and replay exactly that. A single
part plus a moving container is *promoted* to a Scene for this reason. Verified headlessly:
scrubbing `t` moves all four bodies of `examples/container-tilt.json`, bowl included, the
bowl reading 135° of tip at t = 0 and 0° once the ramp completes at t ≈ 0.73.

Costs ~5 ms per simulated second to drive. Preview the **Drop**, not the bowl — the bowl's
own preview is a static ghost of it.

Open: the motion is one rigid track for the whole container rig (several shapes wired into
`container` move together — no per-body motion); there is no way to key an arbitrary path
beyond one translation + one rotation; and nothing couples the motion back to the parts'
sleep detection, so a long shake simulates every frame of itself.

### 2b-ter. `Animate` — the motion on its own, with no physics

The Motion node earned a second consumer. Half of what people want out of a moving part
has no physics in it at all: a lid unscrewing off a jar, a drawer sliding out, a door
swinging, a part lifted clear of an assembly to show how it comes apart. Nothing falls,
nothing collides, nothing is carried by friction — the shape simply GOES where you said,
and `t` scrubs it. **Drop asks what would happen; Animate says do this.**

It reuses everything and invents nothing:

- **The plan and the driver are the same.** `_animate` calls `_motion_driver` with an
  identity bed frame (`B = I`, `o = 0`) and the shape's own tessellated bbox centre as the
  pivot — tessellated for the same reason `PlaceOnBed` is, since the OCCT box is oversized
  and an off-centre axis is the one thing an unscrewing lid cannot tolerate.
- **A screw needed no new vocabulary.** Translation and rotation already ride ONE phase,
  so `move z 12` + `rotate z 720` at `cycles 0` is a helix: the cap rises exactly as fast
  as it turns. That is pinned by a test, because two clocks would look almost right.
- **It bakes keyframes instead of staying analytic.** `pose(tau)` is sampled at 60 Hz (at
  least 24 samples per oscillation cycle, capped at 2000) and shipped as a `kind:"keys"`
  `_noodle_anim` — the format collide scenes already use, which the browser already
  replays at 60 fps through `sceneBodyPose`. So the whole frontend cost was replacing two
  `cadType === 'Drop'` tests with a `TIMELINE_NODES` set; without that Animate would be
  correct on disk and silently un-scrubbable on screen. Measured in a real browser:
  scrubbing its own `t` gives 60 fps replay plus exactly one exact re-bake when the drag
  settles, and a Number Slider wired into `t` gives zero engine runs.
- **It must not copy Drop's un-fan.** A Drop gathers its shapes into one scene because
  they collide with each other. Nothing here interacts, so five lids wired in are five
  independent movements — the ordinary fan-out rule, with a test pinning it. Likewise no
  `container`, `collide`, `grip`, `material` or `settle`: everything that costs compute
  stays in Drop, and the two share one Motion node and can share one `t` slider.

**`hold`, and why it is not a Remap.** The point of sharing a Motion is sharing a
*clock*: one slider that unscrews a cap and then tips the jar. That needs the two
timelines to be the same LENGTH, and the obvious way — rescale `t` through a `Remap`
between the slider and the Animate — silently costs the live scrub, because
`applyDropTargets` follows only DIRECT links from the dragged value node into a `t`
socket. It cannot evaluate an intermediate node, and teaching it to would mean
reimplementing engine math in JavaScript, which is exactly the drift this codebase keeps
paying to avoid. So Animate pads its own timeline instead: `hold` adds stillness after
the motion ends, the wire stays direct, and past `end` the phase already parks (at the
destination for a ramp, at the start for an oscillation) so the padding is free and
exact. **Pad the short clock; never rescale the wire.**

`ContainerMotion` is consequently labelled just **Motion** now. The type string is
unchanged — saved graphs, `_container_motion` and every test keep working; only the label
and the search aliases moved.

Examples: `examples/jar-cap-unscrew.json` is the bare mechanism. `examples/threaded-jar-pour.json`
is what it is for — one slider, two clocks: the golden cap unscrews (0.4 s delay + 0.8 s
motion + 6.8 s hold = 8.0 s) and then the glass jar tips 150° (Drop + container motion,
T = 7.9875 s) and pours six rainbow bolts onto the bed 60 mm below. Verified in a real
browser: dragging that single slider moves the cap and all seven scene bodies at 60 fps
with exactly one re-bake at settle. That example was also rebuilt in the process — its
cylinders were `centered: false`, which is MIN-aligned on **all three** axes, so the jar's
bore sat 6 mm off its own axis. Invisible until a threaded cap had to mate with it.

## 3. How each number is got

- **Overhang** — a face needs support if its normal points down more steeply than the
  critical angle (45° by default). **The faces resting ON the bed are excluded**: a flat
  base points straight down too, and counting it as an overhang is the classic way to get
  this backwards — it makes the one orientation that needs *no* support at all look like
  the worst option on the list. (Measured on the example bracket: 1150 mm² of "overhang"
  that is simply the plate, sitting on the glass.)
- **Support volume** — the real one, and it is a **sweep and a boolean**: drop a prism from
  every overhanging triangle to the bed, union them (`manifold3d.batch_boolean`), and
  subtract the part *plus the part shifted down by the clearance gap* — that second copy is
  what carves the space the support must leave under the face, or it welds itself on. What
  is left **is** the support: preview it, weigh it, export it.
  Checked against a pencil: a sphere of r=20 sitting on the bed returns **1.63 cm³** where
  the integral gives **1.73** (the difference is tessellation plus the clearance gap). It
  costs ~0.04 s at 1.3k triangles and **0.58 s at 20k**.
  `Σ area × height above the bed` is the cheap **proxy**, and it is not the same quantity
  and cannot be: it counts the column under an overhang even where the part itself is
  already sitting in the way. `OrientForPrint` uses the real volume while the part is under
  `exact_below` triangles and the proxy above it — **all or nothing**, and the report says
  which. Scoring one pose by volume and the next by a proxy would rank two different
  quantities against each other and call it a decision.
- **The weak plane** — `manifold3d`'s `slice(z).area()`, 48 heights up the part. ~0.01 s
  for 80 sections, so scoring a hundred candidate poses is free. A non-watertight mesh has
  no honest cross-section: the report says so and points at `MeshFix` rather than
  inventing a number.
- **Stable poses** — every face of the **convex hull** whose polygon contains the centre
  of mass, projected down. That is what "stable" means: let go of the part and it stays.
  (trimesh ships `compute_stable_poses`, but it needs `networkx` and `shapely`, which are
  not in the image. The convex hull is `scipy`, which is.) Cluster the hull's faces by
  normal with a **tolerance**, not by a rounded key: one flat side of a part is many
  triangles whose normals agree to 6 decimals and not to 3, and a rounded key splits them
  into two identical "poses" that then both appear in the report.
- **Strength** — with a `load` vector wired in, it is `|load · Z|` after the rotation:
  0 means the load lies in the layer plane, 1 means it hits the layer bond head-on. With
  no load declared, the proxy is the largest weak plane (maximise the smallest glued
  section).

## 4. What the example shows (`examples/print-orientation.json`)

A bracket: a plate, a stem, a hole. Two copies, side by side.

| | as modelled | as oriented (load along the stem) |
|---|---|---|
| support | **none at all** | 0.80 cm³ — about 1 g of PLA |
| bed contact | 1150 mm² | 120 mm² |
| height | 49 mm | 40 mm |
| weak plane | **64 mm²** at the stem root | 88 mm² |
| strength (load across the layers) | **1.00** — head-on | **0.00** |

Printed as modelled it needs **not one support** — and it is standing in the one
orientation that puts its job at right angles to its strength. `OrientForPrint` lays it
down, paying a gram of support (the grey body under the part *is* that gram — you can look
at it) and nine tenths of the bed contact, and it is right to: supports are money, a
snapped bracket is a bracket.

**Then unwire the `load`** and it puts the part back upright. Nothing broke — you asked a
different question and got an honest answer to it. That is the lesson worth keeping: an
optimiser will hand you the weakest possible part if you never tell it what the part is
FOR.

## 5. Not built yet

- **Bridges** — an overhang spanning two supported walls does not need support at all, and
  `SupportVolume` will happily build a prism under it. The face test cannot see a bridge;
  a per-layer island analysis can. This is the biggest remaining lie in the number.
- **Support that lands on the part, not on the bed** — every prism goes down to z=0. A real
  slicer stops at whatever is underneath, so this OVER-counts wherever the part overhangs
  itself (the part gets subtracted, so the material is right, but the columns still start
  from the bed rather than resting on the shelf below).
- **Support that is not solid** — real support is a sparse lattice at 10-20% density. The
  volume here is the envelope; multiply by your density for the grams that actually go
  through the nozzle.
- **Print time** — height is a proxy. The honest number is `Σ layer perimeter + infill`,
  which is a slicer, and noodle is not one.
- **A real load case** — one vector is one load. A bracket in bending wants a moment, not
  a force. The step after that is an FEA, and the step after *that* is admitting the layer
  bond needs a material model per printer.
