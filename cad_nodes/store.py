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

    def save(self, graph_id: str, graph: Graph, description: str = "") -> None:
        stamp_agent_tags(graph.nodes)
        d = self.dir(graph_id)
        d.mkdir(parents=True, exist_ok=True)
        (d / "graph.json").write_text(json.dumps(graph.to_dict(), indent=2))
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
        mpath.write_text(json.dumps(meta, indent=2))

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
