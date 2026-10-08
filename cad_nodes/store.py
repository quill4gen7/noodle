"""
GraphStore — filesystem persistence for node graphs.

Layout (one directory per graph, shared with the REST server's projects dir):
    <root>/<graph_id>/graph.json   # the graph
    <root>/<graph_id>/meta.json    # {"backend": "nodegraph", ...}
    <root>/<graph_id>/output.stl   # last execution
    <root>/<graph_id>/view.json    # last execution view

The default root is taken from $CAD_PROJECTS_DIR, falling back to /app/projects
(the Docker volume) so REST and MCP operate on the same graphs.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import shutil
from pathlib import Path

from .graph import Graph
from .graph_version import check_base, current_version, write_graph
from .job_files import atomic_write

DEFAULT_ROOT = os.environ.get("CAD_PROJECTS_DIR", "/app/projects")

# Curated example graphs bundled in the repo (tracked in git; projects/ is not,
# so they reach a fresh install through seed_examples() below).
EXAMPLES_DIR = Path(__file__).parent / "examples"

_EXAMPLE_DESCRIPTIONS = {
    "rounded-box": "Hello-world: a box with every edge filleted (primitive → modifier).",
    "flange": "Parametric plate with a bore, exported to STEP (booleans + export).",
    "bolt-flange": "Bolt-circle flange: one hole polar-arrayed and subtracted "
                   "wholesale — Grasshopper-style list fan-out.",
    "csg-boolean": "CSG basics: a box minus a sphere (Subtract boolean).",
    "drop-bounce": "Drop: one timeline slider scrubs three materials "
                   "falling side by side — plastic clatters, lead thuds, rubber "
                   "keeps bouncing.",
    "drop-stack": "Drop with collide: three boxes fall into ONE scene — one "
                  "lands, one stacks, and the half-off cube tips over the edge, "
                  "rolls down the wall and lands flat on the bed beside.",
    "container-tilt": "Drop with a container that MOVES: the balls land in the "
                      "bowl, then it tips over its own rim and pours them out. "
                      "The motion is dictated, not simulated — the balls answer "
                      "to it through contact and friction alone.",
    "threaded-jar-pour": "One slider, two clocks: the threaded cap UNSCREWS "
                         "(Animate — kinematics, no physics) and then the glass "
                         "jar tips and pours six bolts onto the bed below "
                         "(Drop + a moving container — real dynamics).",
    "jar-cap-unscrew": "Animate: the same Motion node, with no physics at all — "
                       "a threaded cap rises off the jar as it turns, on a "
                       "timeline you scrub. Kinematics, not simulation.",
    "drop-in-bowl": "Drop with a container: three balls poured into a bowl that "
                    "never moves. The bowl is the one body kept CONCAVE, so it "
                    "cradles them instead of shedding them.",
    "glass-bowl": "The finishes, earning their keep — a glass bowl with a glowing "
                  "marble dropped inside. The glass is real transmission (the scene "
                  "refracts through it) and the glow really crosses it, which takes "
                  "a dedicated bloom pass. Restyle it all you like: looks live "
                  "outside the graph, so not one node re-runs.",
    "polyhedron-tumbler": "A polyhedron hollowed the one way that works — shell the "
                          "CLOSED solid and pick the opening on it (never delete the "
                          "face first) — then handed to Drop as a container and "
                          "tumbled by a Motion, marbles rattling inside the cavity "
                          "that concave colliders preserve.",
    "wind-drop": "A gust across the bed: four shapes of the same material "
                 "dropped into moving air. The plate and the card tumble, catch "
                 "the wind and skid; the ball barely notices. Drag is measured "
                 "on each part's real silhouette, which changes as it turns.",
    "wind-tunnel": "The wind tunnel: a real Lattice-Boltzmann solve around the "
                   "part, drawn as streamlines. A blunt shape and a faired one "
                   "side by side — one leaves a dead wake, the other keeps the "
                   "flow attached, and the report says by how much.",
    "galton-board": "The normal distribution, fallen out of gravity: 60 balls "
                    "down a grid of diamond pegs, with a blade you slide to bend "
                    "the whole distribution. Turn `grip` up and watch the bell die.",
    "parametric-gear": "Custom node from scratch — a spur gear written in one "
                       "CodeBlock, driven by #@param knobs.",
    "gear-row-fanout": "Grasshopper-style fan-out — a Range feeds the gear "
                       "CodeBlock so it produces a whole row of gears.",
    "scatter-surface": "DivideSurface + fan-out — a stud scattered on every point "
                       "of a U×V grid over a sphere.",
    "voronoi-panel": "Voronoi2D — scattered points become cells, extruded and "
                     "subtracted from a plate into a perforated panel.",
    "voronoi-vase": "Advanced combo — Voronoi cells mapped onto a revolved "
                    "surface, shelled into a thin-walled vase.",
    "voronoi-3d-lattice": "TRUE 3D Voronoi — points scattered INSIDE a sphere "
                          "become convex mesh cells, shrunk and subtracted so "
                          "the walls between them are the part: an organic "
                          "lattice (Populate volume fill + Voronoi3D).",
    "parametric-curves": "Parametric curves — a Spline through points, an "
                         "ArcCenter and a Line as building blocks for wire geometry.",
    "predicate-selectors": "Selecting by RULE, not by clicking — EdgesByType picks "
                           "every circular edge and fillets it, so the selection "
                           "survives a change of geometry that a hand-picked list "
                           "would not.",
    "lego-brick": "Array fan-out showcase — one stud becomes a grid via two "
                  "Linear Arrays, fused with Union and rounded into a "
                  "recognizable LEGO brick. Grouped into 4 labelled stages.",
    "softmax": "Maths made geometry — the softmax function wired node by node "
               "(z/T → exp → sum → normalise), with three rows of bars and a pie "
               "chart that redraw live as you drag the logits and the temperature. "
               "Drag `t` with `onda` > 0 to animate the winner travelling between "
               "classes.",
    "gradient-descent": "How a machine learns, as geometry — a loss surface, and "
                        "the descent path as a chain of spheres. Drag `k` to roll the "
                        "ball down; raise the learning rate until the path zig-zags "
                        "out of the valley; drag the starting point (it carries a "
                        "gizmo) into the wrong basin and watch it settle in a local "
                        "minimum. The gradient is numeric, so the algorithm knows "
                        "nothing about f — change f and it still works.",
    "perceptron": "The first machine that learned anything (1958) — it is never told "
                  "the rule, only told when it guessed wrong, and it leans its line "
                  "towards the point it missed. Drag `epochs` and the red mistakes "
                  "wink out one by one until it converges and stops for good. Then add "
                  "`noise`: no line can be right any more, and it never settles.",
    "l-system": "One letter and one rewrite rule, and a tree grows — F -> F[+F]F[-F][F], "
                "applied over and over, then read by a turtle. Drag `depth` and a whole "
                "generation of twigs appears; drag `angle` and you change the species. "
                "Every branch is a real tapered cone, so this one you can print.",
    "convolution": "Nine numbers that can blur a picture, sharpen it, or find every edge "
                   "in it — the image as a field of columns, the 3x3 kernel beside it, "
                   "the answer on the right. Edge detect returns exactly zero wherever "
                   "the picture is flat, so only the outline survives. It is what a "
                   "vision network's first layer does; the network just learns the nine.",
    "riemann-sums": "The integral, caught in the act of being invented — chop the area "
                    "under a curve into n rectangles and read how wrong you are. Drag n "
                    "and watch the error fall; switch from the left edge to the midpoint "
                    "and it collapses at the same n. Even the 'exact' value is a "
                    "staircase, just a very fine one.",
    "attention": "The sequel to `softmax` — attention is the same function taken one "
                 "ROW at a time. The blue grid is the raw scores q.k (negative bars "
                 "hang below the plane); the green grid is after the softmax, where "
                 "every row sums to exactly 1. Switch `causal` on and the upper "
                 "triangle vanishes: a token may not look at the future — and each "
                 "row still sums to 1.",
    "cellular-automata": "Eight bits of program, and a universe — an elementary "
                         "cellular automaton whose generations stack along Z into a "
                         "printable tower of time. Rule 90 is a Sierpinski triangle, "
                         "rule 30 is chaos used as a random generator, rule 110 is "
                         "Turing complete. One live cell to start with.",
    "de-casteljau": "How a Bezier is actually built: not a polynomial, just "
                    "interpolation repeated until one point is left. Drag `t` and the "
                    "ladder collapses onto the curve; drag a control point (they carry "
                    "gizmos) and watch the curve get pulled towards it without ever "
                    "passing through it.",
    "matrix-determinant": "Linear algebra you can hold — a 3x3 matrix (nine sliders) "
                          "deforms a unit cube, and the catalog's Volume node reads "
                          "the DETERMINANT off the solid. Shear it and the volume "
                          "does not move; flatten a row and det hits 0; flip a sign "
                          "and space turns inside out. The three arrows are the "
                          "matrix's columns.",
    "central-limit": "Why the bell curve shows up uninvited — average n uniform "
                     "numbers (a perfectly flat distribution), 4000 times, and plot "
                     "the histogram. n=1 is flat; by n=3 it is a bell. The width "
                     "shrinks as 1/sqrt(n). The yellow curve is the gaussian the "
                     "theorem predicts, not a fit.",
    "fourier-epicycles": "Any wave is a sum of circles — each harmonic is a circle "
                         "riding on the tip of the last one, and the pen traces the "
                         "wave, unrolled in time on the right. Drag `t` to turn the "
                         "wheels; add harmonics and watch a square wave sharpen (the "
                         "ripples that never leave are Gibbs).",
    "kmeans-voronoi": "Finding groups nobody labelled — Lloyd's algorithm: assign "
                      "each point to the nearest centroid, move each centroid to the "
                      "mean of its points, repeat. Drag `iterations` from 0 and watch "
                      "them migrate. The catalog's Voronoi2D on the centroids gives "
                      "the decision regions for free.",
    "nucleus-sampling": "How a language model picks the next word — softmax gives the "
                        "probabilities, but someone still has to CHOOSE. Top-p walks "
                        "down the sorted bars adding up probability and stops at p, so "
                        "the number of words it keeps is decided by the model's "
                        "confidence, not by you: same p, one word after 'the capital of "
                        "France is', a dozen after 'she opened the door and saw'. "
                        "Top-k cannot do that.",
    "neural-network": "What 'a billion parameters' actually looks like — one sphere per "
                      "neuron, one cylinder per weight, radius proportional to |w|. "
                      "Double a layer's width and the wires QUADRUPLE (weights are a "
                      "product, not a sum). Then drag `prune` and watch most of them "
                      "vanish while the object still looks like itself: a trained "
                      "network is mostly near-zero weights, which is why pruning works.",
    "aliasing": "The wave that was never there — a sine, and a clock that looks at it fs "
                "times a second. The ghost is the slowest wave through every sample, and "
                "it agrees with the evidence EXACTLY, so nothing downstream can tell them "
                "apart. Below fs/2 the ghost IS the wave (that congruence is the sampling "
                "theorem); above it, 7 Hz arrives as a calm, innocent 3 Hz.",
    "overfitting": "It knows the answers and not the question — a degree-d polynomial "
                   "through noisy points. The error on what it was SHOWN falls forever; "
                   "the error on what it was NOT shown bottoms out around degree 5 and "
                   "then climbs. At degree = n-1 the curve hits every training point "
                   "exactly, scores zero, and has learned nothing but the noise. Ridge "
                   "tames the monster without taking a single coefficient away.",
    "sorting": "The same answer, at four different prices — bubble, insertion, "
               "selection and quicksort on the same twelve bars. `step` scrubs through "
               "the run one COMPARISON at a time (the pair under the eye is drawn in its "
               "own colour), and the four columns are the bill. At twelve numbers nobody "
               "cares; the Panel quietly runs all four on 200 and the polite little gap "
               "becomes 19,834 comparisons against 1,510.",
    "dijkstra": "The same loop, with one line changed: which cell do you open next? "
                "Dijkstra takes the cheapest so far and spreads in a circle (439 cells "
                "of 484). A* adds a guess at what is left, opens a corridor instead — "
                "187 cells — and finds the IDENTICAL path, because the guess never "
                "over-promises. Greedy drops the cost-so-far, opens 43, and walks "
                "straight over the mountain for a path 34% worse.",
    "perlin-noise": "Random, but not RANDOM — white noise is static and always will be, "
                    "because no two neighbouring points were ever made to agree. Perlin "
                    "puts a random DIRECTION at each grid corner instead of a random "
                    "number at each point, so nearby points are FORCED to agree, and out "
                    "comes a landscape. Octaves add detail at half the height; the sea is "
                    "just a plane. Built on the mesh lane, watertight, and printable.",
    "print-orientation": "Which way up is not a convenience — it decides where the part "
                         "breaks. The same bracket, printed two ways: as modelled it "
                         "needs NOT ONE support, and its weakest glued section (64 mm2, "
                         "drawn in red at the stem root) is exactly where the load will "
                         "snap it off. Wire the load direction into `Orient for Print` "
                         "and it lays the part down instead — a gram of support you can "
                         "SEE (`Support Volume` builds the body, it does not estimate "
                         "it), and a bracket that holds. Unwire the load and it goes "
                         "back to the "
                         "weak one: an optimiser hands you the worst part in the world "
                         "if you never tell it what the part is FOR.",
    "mesh-lane": "The mesh lane — a Box and a Sphere tessellate into triangles just "
                 "by touching a mesh input, get cut with a mesh boolean (manifold3d: "
                 "0.1s where the B-Rep kernel needs 81s), simplified within a bounded "
                 "tolerance and inspected. Note the Sphere rides the SAME Move node "
                 "the B-Rep lane uses. build123d cannot model meshes at all — see "
                 "PLAN_MESH_LANE.md.",
    "bolt-and-nut": "The Thread node, both ways round — a bolt whose thread ADDS to "
                    "its shank and a nut whose internal thread (really the TAP) CUTS "
                    "the hole and the thread in one boolean. Set `clearance` on ONE "
                    "half of the pair, or you print double the gap.",
    "pipe-fitting": "Threads from first principles — a male and a female pipe "
                    "fitting whose ribs are built by hand: Helix, a swept profile, "
                    "a boolean. The Thread node does this in one node now; this is "
                    "the graph to open when the profile you need is not one of its "
                    "four families.",
    "axle-cage": "The biggest graph in the gallery, and a real part — the plates "
                 "of an axle cage drawn as curves on planes, extruded, and finished "
                 "by rule: selectors pick the edges, Fillet/Chamfer act on what "
                 "they picked, and Center of Mass reads the result. Eighty nodes "
                 "of production CAD, not a demo.",
    "retroeng-motor-mount": "Retro-engineering, the finished article — a motor mount "
                            "rebuilt as a PARAMETRIC graph from a 59k-triangle scan "
                            "the agent could only slice and measure (volume within "
                            "2.2%). Arrays where the scan had repetition, sliders "
                            "where it had dimensions: procedure, not tracing.",
    "jacobian-conjecture": "The Jacobian conjecture (Keller, 1939), as geometry — a "
                           "polynomial map deforms space while its Jacobian, the "
                           "LOCAL volume zoom, stays one constant everywhere. The "
                           "conjecture says such a map can never fold two points "
                           "onto one. Open the sequel to see how that ended.",
    "jacobian-counterexample": "How it ended (Alpoge-Fable, July 2026): 216 "
                               "characters whose Jacobian is EXACTLY -2 at every "
                               "point of space — and three points that land on the "
                               "same destination anyway. Eighty-seven years of "
                               "conjecture, disproved in one picture: three curves "
                               "meeting where none was allowed to.",
}

# A graph id is a single directory name under the store root. Rejecting
# anything else (path separators, "..", hidden names) closes path traversal
# for every surface that resolves ids to paths (REST, MCP, copilot).
_GRAPH_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,63}$")


def validate_graph_id(graph_id: str) -> str:
    """Return graph_id if it is a safe directory name, else raise ValueError."""
    if not isinstance(graph_id, str) or not _GRAPH_ID_RE.fullmatch(graph_id):
        raise ValueError(
            f"Invalid project name {graph_id!r}: use 1-64 chars of letters, "
            "digits, '.', '_', '-' or spaces, starting with a letter or digit"
        )
    return graph_id


def stamp_agent_tags(nodes) -> None:
    """Fill the empty `date` param of ToAgent tag nodes at save time — the
    provenance date the agent searches by ('il pezzo messo lì ieri'). Accepts
    Node objects or plain node dicts, so both the GraphStore and the REST
    save route can stamp."""
    today = datetime.date.today().isoformat()
    for n in nodes:
        is_dict = isinstance(n, dict)
        ntype = n["type"] if is_dict else n.type
        if ntype != "ToAgent":
            continue
        params = n.setdefault("params", {}) if is_dict else n.params
        if not params.get("date"):
            params["date"] = today


class GraphStore:
    def __init__(self, root: str | Path = DEFAULT_ROOT):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def dir(self, graph_id: str) -> Path:
        return self.root / validate_graph_id(graph_id)

    def exists(self, graph_id: str) -> bool:
        return (self.dir(graph_id) / "graph.json").exists()

    def list(self) -> list[str]:
        return sorted(
            d.name for d in self.root.iterdir()
            if d.is_dir() and (d / "graph.json").exists()
        )

    def load(self, graph_id: str) -> Graph:
        gpath = self.dir(graph_id) / "graph.json"
        if not gpath.exists():
            raise KeyError(f"No graph {graph_id!r}")
        return Graph.from_dict(json.loads(gpath.read_text()))

    def version(self, graph_id: str) -> str | None:
        """The on-disk version of graph.json (a content hash; None if absent)."""
        return current_version(self.dir(graph_id) / "graph.json")

    def save(self, graph_id: str, graph: Graph, description: str = "",
             base_version: str | None = None) -> str:
        """Write the graph; return its new version.

        With `base_version`, refuse (StaleGraphError) if the file on disk is no
        longer that version — someone else wrote it meanwhile. See graph_version.
        """
        d = self.dir(graph_id)
        check_base(d / "graph.json", base_version)
        stamp_agent_tags(graph.nodes)
        d.mkdir(parents=True, exist_ok=True)
        version = write_graph(d / "graph.json", json.dumps(graph.to_dict(), indent=2))
        meta = {}
        mpath = d / "meta.json"
        if mpath.exists():
            try:
                meta = json.loads(mpath.read_text())
            except Exception:
                meta = {}
        meta["backend"] = "nodegraph"
        if description:
            meta["description"] = description
        atomic_write(mpath, json.dumps(meta, indent=2))
        return version

    def delete(self, graph_id: str) -> None:
        d = self.dir(graph_id)
        if d.is_dir():
            shutil.rmtree(d)

    def seed_examples(self) -> list[str]:
        """On a fresh store (no projects yet, never seeded), copy the bundled
        example graphs in so a new user lands on real graphs instead of a blank
        canvas. Idempotent and non-destructive: a `.seeded` marker means we
        never re-add examples the user has since deleted, and an existing
        project short-circuits it entirely. Returns the names seeded."""
        marker = self.root / ".seeded"
        if marker.exists() or self.list():
            return []
        seeded: list[str] = []
        for jp in sorted(EXAMPLES_DIR.glob("*.json")):
            name = jp.stem
            try:
                validate_graph_id(name)
                graph = Graph.from_dict(json.loads(jp.read_text()))
                self.save(name, graph, _EXAMPLE_DESCRIPTIONS.get(name, ""))
                seeded.append(name)
            except Exception:
                continue  # a broken example must never break startup
        try:
            marker.write_text("")
        except OSError:
            pass
        return seeded

    def view(self, graph_id: str) -> dict | None:
        vpath = self.dir(graph_id) / "view.json"
        if not vpath.exists():
            return None
        try:
            return json.loads(vpath.read_text())
        except Exception:
            return None

    # --- generations: frozen snapshots of a run, for the read-only viewer ------
    # A generation is a COPY of one run's view.json + the graph.json that made it,
    # under <graph_id>/gens/g<N>/. It exists so a link sent to someone (an agent
    # reporting "look at this" instead of a screenshot) keeps showing THAT result
    # while the live workflow moves on. Written once, never modified: the numbers
    # are never reused, so /view/<name>/g7 means the same thing forever (until the
    # project itself is deleted).
    GENS_DIRNAME = "gens"

    def gens_dir(self, graph_id: str) -> Path:
        return self.dir(graph_id) / self.GENS_DIRNAME

    def gen_dir(self, graph_id: str, gen: str) -> Path:
        return self.gens_dir(graph_id) / validate_gen_id(gen)

    def list_gens(self, graph_id: str) -> list[dict]:
        """Every generation's meta, newest first."""
        root = self.gens_dir(graph_id)
        out = []
        if root.is_dir():
            for d in root.iterdir():
                if not (d.is_dir() and _GEN_ID_RE.fullmatch(d.name)):
                    continue
                try:
                    out.append(json.loads((d / "meta.json").read_text()))
                except (OSError, ValueError):
                    continue          # a half-written gen is not listed
        out.sort(key=lambda m: int(m.get("gen", "g0")[1:]), reverse=True)
        return out

    def save_gen(self, graph_id: str, view: dict, graph: dict, meta: dict) -> dict:
        """Freeze `view` + `graph` as the next generation; return its meta."""
        root = self.gens_dir(graph_id)
        root.mkdir(parents=True, exist_ok=True)
        n = max([int(d.name[1:]) for d in root.iterdir()
                 if _GEN_ID_RE.fullmatch(d.name)] or [0]) + 1
        while True:                   # mkdir is the atomic claim on the number
            d = root / f"g{n}"
            try:
                d.mkdir()
                break
            except FileExistsError:
                n += 1
        meta = {**meta, "gen": d.name, "graph": graph_id}
        (d / "view.json").write_text(json.dumps(view))
        (d / "graph.json").write_text(json.dumps(graph, indent=2))
        # meta LAST, and atomic: list_gens only lists a gen whose meta exists, so a
        # reader never sees one whose view is still being written, nor half a meta.
        atomic_write(d / "meta.json", json.dumps(meta, indent=2))
        return meta

    # Two small files sit NEXT TO a generation without changing it (its view,
    # graph and meta are immutable and cached a year): `thumb.jpg`, the card
    # picture, drawn once by whichever page first renders the gen, and
    # `seen.json`, when the user last opened it in /view — which is how an agent
    # asked "this one" finds out which of its proposals the user is looking at.
    GEN_THUMB = "thumb.jpg"
    GEN_SEEN = "seen.json"

    def gen_extras(self, graph_id: str, gen: str) -> dict:
        d = self.gen_dir(graph_id, gen)
        out = {"thumb": (d / self.GEN_THUMB).exists(), "seen": None}
        try:
            out["seen"] = json.loads((d / self.GEN_SEEN).read_text()).get("seen")
        except (OSError, ValueError):
            pass
        return out

    def save_gen_thumb(self, graph_id: str, gen: str, data: bytes) -> bool:
        """Store the card picture once; False if one is already there (the
        first render wins, so a picture never changes under a link)."""
        d = self.gen_dir(graph_id, gen)
        if not (d / "meta.json").exists():
            raise KeyError(f"No generation {gen!r} in {graph_id!r}")
        if (d / self.GEN_THUMB).exists():
            return False
        tmp = d / (self.GEN_THUMB + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(d / self.GEN_THUMB)
        return True

    # `tags.json`: the agent's labels on the pieces of a gen it sends the user
    # ("coperchio v2", "foro M8 qui") — beside the gen like seen.json, so the
    # gen itself stays immutable and the tags can be rewritten.
    GEN_TAGS = "tags.json"

    def save_gen_tags(self, graph_id: str, gen: str, tags: list) -> None:
        d = self.gen_dir(graph_id, gen)
        if not (d / "meta.json").exists():
            raise KeyError(f"No generation {gen!r} in {graph_id!r}")
        atomic_write(d / self.GEN_TAGS, json.dumps({"tags": tags}, indent=1))

    def load_gen_tags(self, graph_id: str, gen: str) -> list:
        d = self.gen_dir(graph_id, gen)
        if not (d / "meta.json").exists():
            raise KeyError(f"No generation {gen!r} in {graph_id!r}")
        try:
            return json.loads((d / self.GEN_TAGS).read_text()).get("tags") or []
        except (OSError, ValueError):
            return []

    # `measures.json`: the agent's dimensions on a gen (📏, PLAN_VIEW_MEASURE.md)
    # — a file of its own beside tags.json: separate validation, separate toggle.
    GEN_MEASURES = "measures.json"

    def save_gen_measures(self, graph_id: str, gen: str, measures: list) -> None:
        d = self.gen_dir(graph_id, gen)
        if not (d / "meta.json").exists():
            raise KeyError(f"No generation {gen!r} in {graph_id!r}")
        atomic_write(d / self.GEN_MEASURES, json.dumps({"measures": measures}, indent=1))

    def load_gen_measures(self, graph_id: str, gen: str) -> list:
        d = self.gen_dir(graph_id, gen)
        if not (d / "meta.json").exists():
            raise KeyError(f"No generation {gen!r} in {graph_id!r}")
        try:
            return json.loads((d / self.GEN_MEASURES).read_text()).get("measures") or []
        except (OSError, ValueError):
            return []

    def mark_gen_seen(self, graph_id: str, gen: str, when: str) -> None:
        d = self.gen_dir(graph_id, gen)
        if not (d / "meta.json").exists():
            raise KeyError(f"No generation {gen!r} in {graph_id!r}")
        atomic_write(d / self.GEN_SEEN, json.dumps({"seen": when}))

    # Notes for the agent: what the user DREW on a generation in /view (strokes
    # on the surface, a colour, a sentence — "this hole could be better"). They
    # sit beside the gen like seen.json, in `notes/`, one JSON + one JPEG (the
    # user's own view with the strokes on it) per note. Bound to a GENERATION on
    # purpose: the stroke coordinates mean something only on the geometry they
    # were drawn on, and the gen keeps that geometry (and its graph) forever.
    GEN_NOTES = "notes"

    def gen_notes_dir(self, graph_id: str, gen: str) -> Path:
        d = self.gen_dir(graph_id, gen)
        if not (d / "meta.json").exists():
            raise KeyError(f"No generation {gen!r} in {graph_id!r}")
        return d / self.GEN_NOTES

    def list_gen_notes(self, graph_id: str, gen: str) -> list[dict]:
        """The gen's notes, oldest first (a1, a2, …)."""
        d = self.gen_notes_dir(graph_id, gen)
        out = []
        if d.is_dir():
            for p in d.glob("a*.json"):
                if not _NOTE_ID_RE.fullmatch(p.stem):
                    continue
                try:
                    out.append(json.loads(p.read_text()))
                except (OSError, ValueError):
                    continue
        out.sort(key=lambda n: int(n.get("id", "a0")[1:]))
        return out

    def save_gen_note(self, graph_id: str, gen: str, note: dict,
                      jpeg: bytes | None = None,
                      view_jpegs: list[bytes | None] | None = None,
                      images: list[tuple[str, bytes]] | None = None,
                      note_id: str | None = None) -> dict:
        """Store a note; returns it with its id. Image first, JSON last and
        atomic, so a listed note always has its picture.

        With `note_id` the note is REWRITTEN in place: /view saves as the user
        draws, so one sitting is one note however often it changes (feedback
        20261008-153010: a draft that lived only in the page was lost on a
        reload). `created` is kept, `updated` set, and a note the agent had
        closed is reopened — the user changed it after the reply."""
        import datetime
        d = self.gen_notes_dir(graph_id, gen)
        d.mkdir(exist_ok=True)
        if note_id is not None:
            nid = validate_note_id(note_id)
            p = d / f"{nid}.json"
            if not p.exists():
                raise KeyError(f"No note {note_id!r} on {graph_id}/{gen}")
            old = json.loads(p.read_text())
            note = {**note, "created": old.get("created") or note.get("created"),
                    "updated": datetime.datetime.now().isoformat(timespec="seconds")}
            if old.get("done"):
                note["reopened"] = old["done"]
        else:
            # `aN.claim` is the atomic claim on the id and is KEPT, even when the
            # note is deleted: like a gen number, `graph/gN#aK` means one note forever.
            n = max([int(p.stem[1:]) for p in d.glob("a*.claim")
                     if _NOTE_ID_RE.fullmatch(p.stem)] or [0]) + 1
            while True:
                try:
                    (d / f"a{n}.claim").open("x").close()
                    break
                except FileExistsError:
                    n += 1
            nid = f"a{n}"
        note = {**note, "id": nid, "gen": gen, "graph": graph_id, "image": bool(jpeg)}
        if jpeg:
            (d / f"{nid}.jpg").write_bytes(jpeg)
        for k, data in enumerate(view_jpegs or [], 1):
            if data:
                (d / f"{nid}.v{k}.jpg").write_bytes(data)
        # pictures the user PLACED on the part (✎ Disegna → Immagine): assets of
        # the note, named in its `images` so the agent can open them
        for k, (ext, data) in enumerate(images or [], 1):
            (d / f"{nid}.img{k}.{ext}").write_bytes(data)
            note["images"][k - 1]["file"] = f"{nid}.img{k}.{ext}"
        atomic_write(d / f"{nid}.json", json.dumps(note, indent=1))
        if note_id is not None:                # what the rewrite no longer names
            keep = {f"{nid}.json", f"{nid}.claim"} | ({f"{nid}.jpg"} if jpeg else set())
            keep |= {f"{nid}.v{k}.jpg" for k, data in enumerate(view_jpegs or [], 1) if data}
            keep |= {f"{nid}.img{k}.{ext}" for k, (ext, _) in enumerate(images or [], 1)}
            for f in d.glob(f"{nid}.*"):
                if f.name not in keep:
                    f.unlink(missing_ok=True)
        return note

    def update_gen_note(self, graph_id: str, gen: str, note_id: str, **fields) -> dict:
        p = self.gen_notes_dir(graph_id, gen) / f"{validate_note_id(note_id)}.json"
        if not p.exists():
            raise KeyError(f"No note {note_id!r} on {graph_id}/{gen}")
        note = {**json.loads(p.read_text()), **fields}
        atomic_write(p, json.dumps(note, indent=1))
        return note

    def delete_gen_note(self, graph_id: str, gen: str, note_id: str) -> None:
        d = self.gen_notes_dir(graph_id, gen)
        p = d / f"{validate_note_id(note_id)}.json"
        if not p.exists():
            raise KeyError(f"No note {note_id!r} on {graph_id}/{gen}")
        p.unlink()
        (d / f"{note_id}.jpg").unlink(missing_ok=True)
        for v in d.glob(f"{note_id}.v*.jpg"):
            v.unlink()
        for v in d.glob(f"{note_id}.img*"):
            v.unlink()

    def gen_note_asset(self, graph_id: str, gen: str, note_id: str, k: int) -> Path:
        """The k-th picture placed on the part with note `note_id` (1-based)."""
        d = self.gen_notes_dir(graph_id, gen)
        hits = sorted(d.glob(f"{validate_note_id(note_id)}.img{int(k)}.*"))
        if not hits:
            raise KeyError(f"No image {k} on note {note_id!r} of {graph_id}/{gen}")
        return hits[0]

    def gen_note_image(self, graph_id: str, gen: str, note_id: str, view: int = 0) -> Path:
        """view 0 = the main picture (the last view); k >= 1 = the k-th other view."""
        name = validate_note_id(note_id) + (f".v{int(view)}" if view else "")
        return self.gen_notes_dir(graph_id, gen) / f"{name}.jpg"

    def load_gen(self, graph_id: str, gen: str, part: str) -> dict:
        """One file of a generation: part is view | graph | meta."""
        if part not in ("view", "graph", "meta"):
            raise ValueError(f"unknown generation part {part!r}")
        p = self.gen_dir(graph_id, gen) / f"{part}.json"
        if not p.exists():
            raise KeyError(f"No generation {gen!r} in {graph_id!r}")
        return json.loads(p.read_text())


_GEN_ID_RE = re.compile(r"^g[1-9][0-9]{0,6}$")


def validate_gen_id(gen: str) -> str:
    if not isinstance(gen, str) or not _GEN_ID_RE.fullmatch(gen):
        raise ValueError(f"Invalid generation id {gen!r}: expected g<number>, e.g. g3")
    return gen


_NOTE_ID_RE = re.compile(r"^a[1-9][0-9]{0,5}$")


def validate_note_id(note_id: str) -> str:
    if not isinstance(note_id, str) or not _NOTE_ID_RE.fullmatch(note_id):
        raise ValueError(f"Invalid note id {note_id!r}: expected a<number>, e.g. a2")
    return note_id
