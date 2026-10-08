// ✎ the pen as an imaginary 3D pen — ink that piles up where you INSIST.
//
// A stroke is still paint ON the part: every sample is a raycast onto the
// pieces, kept with its normal. What this adds is a LIFT per sample (mm along
// that normal): where earlier ink already lies under the pen, the new tube may
// sit on top of it instead of crossing through it — so circling one spot
// builds a little heap («cacchetta») layer by layer.
//
// It must not turn every crossing into a tower, so it happens only where the
// pen INSISTS: the spot must already have been covered by `passes` − 1
// earlier passes (a line crossed once or twice stays flat on the part), and
// then each further pass adds ONE layer (`layer` × width) on top of the
// highest ink under the pen. Along the stroke the lift changes at most `slope`
// mm per mm travelled, so the tube climbs onto a heap and comes down off it
// as a ramp, never as a wall — like a real 3D pen, it may hang briefly in the
// air off the edge of a heap before it settles back on the part.
//
// Pure: no three, no DOM — tests/ui/pen3d.test.cjs. Points are [x, y, z].

export const PEN3D = {
  passes: 3,        // the 3rd pass over a spot is the first that stacks
  layer: 0.7,       // one layer = 0.7 × width (layers overlap, like extruded ink)
  slope: 1,         // ≤ 1 mm of lift change per mm along the stroke (45°)
  reach: 0.6,       // two samples touch when closer than reach × (w1 + w2)
  tail: 2.5,        // the stroke's own last 2.5 widths are the pen, not old ink
  maxLayers: 60,    // a safety cap, never reached by hand in practice
};

const dist = (a, b) => Math.hypot(a[0] - b[0], a[1] - b[1], a[2] - b[2]);

// Arc length of a stroke at each sample (cumulative, from its start).
export function arcs(pts) {
  const out = [0];
  for (let i = 1; i < pts.length; i++) out.push(out[i - 1] + dist(pts[i - 1], pts[i]));
  return out;
}

// The lift the NEXT sample `p` of stroke `cur` gets. `strokes` are all the
// strokes laid so far, CURRENT ONE INCLUDED (its samples so far, without p):
// [{ k, width, pts: [[x,y,z]…], lift: [mm…] }]. `cur` is the current stroke
// (an element of `strokes`, or null when p starts a new one); `w` is p's width.
export function liftAt(p, w, strokes, cur, o = PEN3D) {
  let curArc = 0;
  if (cur && cur.pts.length) {
    const last = cur.pts[cur.pts.length - 1];
    curArc = (cur._arc ? cur._arc[cur._arc.length - 1] : arcs(cur.pts).at(-1)) + dist(last, p);
  }
  // the ink under the pen, measured as the LENGTH of stroke inside the pen's
  // reach, in passes: one pass straight through the spot leaves 2 × reach of
  // it. Counting runs of samples instead miscounts the seam of a circle — its
  // start and its end both lie there, and one loop read as two passes.
  let passes = 0, top = 0;
  for (const s of strokes) {
    const lift = s.lift || [], reach = o.reach * (w + s.width);
    const arc = s === cur ? (s._arc || arcs(s.pts)) : null;
    let was = false;
    for (let i = 0; i < s.pts.length; i++) {
      const is = !(arc && curArc - arc[i] < o.tail * w)      // the pen's own wake
        && dist(p, s.pts[i]) < reach;
      if (is) top = Math.max(top, lift[i] || 0);
      if (is && was) passes += dist(s.pts[i - 1], s.pts[i]) / (2 * reach);
      was = is;
    }
  }
  // − ½: a pass that only grazes the spot leaves less than 2 × reach
  let target = passes >= o.passes - 1.5 ? top + o.layer * w : 0;
  target = Math.min(target, o.maxLayers * o.layer * w);
  // along the stroke: a ramp, never a wall
  if (cur && cur.pts.length) {
    const prevLift = (cur.lift && cur.lift.at(-1)) || 0;
    const step = o.slope * dist(cur.pts.at(-1), p);
    target = Math.min(Math.max(target, prevLift - step), prevLift + step);
  }
  return target;
}

// The heap a stroke built: its highest lift + its own width (the top of the
// tube above the part), 0 when it stayed flat.
export function heightOf(s) {
  const m = Math.max(0, ...(s.lift || []));
  return m > 0 ? m + s.width : 0;
}
