<!--
  noodle PR template. If this PR fixes an in-app feedback report, see
  docs/FEEDBACK_FIX_GUIDE.md and .claude/skills/feedback-fix/SKILL.md.
  Do NOT commit the feedback/ directory — reference the report id only.
-->

## Summary

<!-- What does this PR do, and why? -->

## Source report

<!-- If this came from an in-app feedback drop: feedback/<id>. Otherwise: N/A. -->

- Report id: `feedback/<id>`
- Severity: <!-- bug | idea | question -->

## How to reproduce

<!-- Minimal steps. Attach the graph snapshot if it helps the reviewer. -->

## What changed

<!-- The bug / root cause / the fix. Note any wire-type or API changes. -->

## Tests run

- [ ] `python -m pytest tests/ -v`
- [ ] Engine change verified by transpiling/executing the graph — see CLAUDE.md §2
- [ ] Geometry change LOOKED at: `GET /api/graph/{name}/screenshot` (CLAUDE.md §9)
- [ ] `docker restart noodle` + manual check (if backend changed)
- [ ] Before/after behaviour confirmed

## AI disclosure (required)

> Every AI-assisted PR must disclose this. Refers to the **external coding
> agent**, not the in-app copilot (which by policy does not modify the app).

- **Coding agent(s) used:** <!-- e.g. Claude Code -->
- **Model(s) used:** <!-- e.g. claude-opus-4-8 -->
- **Session / transcript:** <!-- link or attachment, ONLY if scrubbed of secrets,
  private paths, and personal data. Otherwise summarize the approach in words. -->
