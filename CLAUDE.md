# noodle — fork «noodle-deploy-gh»

Stub corto. La guida completa di QUESTO repo è `docs/agent-guide-full.md`:
leggila solo quando lavori sul codice. La maggior parte (90%) è uguale a tutti
i noodle* — non viene riscritta qui.

**Che fork è**: ramo noodle-cb-trace — è il VERTICE della linea della linea noodle.
**Cosa aggiunge**: bump di sicurezza deps-2026-10 (fastapi/starlette/multipart/mcp) + fast-path bboxes (optimal=False, view.bbox approx:true)

## Istruzioni generali (valgono per tutti i noodle*)

- Lavori sul CODICE, non disegnare modelli: per quello c'è la skill `noodle`
  (MCP `cad_*` sul gpunix, `cad_help` prima di tutto).
- La base del vertice (tutte le feature) è la skill `noodle-guide`,
  `references/base.md`. Questo repo è a uno stage precedente: i file citati
  possono non esserci — verifica con `ls` prima di fidarti.
- Run/verify nel container, non sull'host (§1 della guida completa):
  `noodle-compose up -d` per CREARE il container (rootless podman),
  `docker restart noodle` dopo le modifiche al backend, `docker logs -f`.
- Non editare a mano i graph.json dei progetti: usa l'API (§6e della guida).

## Tieni al passo il catalogo

Feature o fork nuovo → aggiorna la tabella in
`~/.claude/skills/noodle-guide/SKILL.md` (pi la legge via symlink
`~/.pi/agent/skills/noodle-guide`).
