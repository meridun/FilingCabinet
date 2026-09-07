# model-repo

Canonical **upstream hub for shared repo configuration** across my project repos — primarily the
parts a repo uses to configure its AI agents (Claude Code / GitHub Copilot): instructions, skills,
agents, hooks, worker prompts, and the scripts that keep them in sync. It is also a GitHub template
(`is_template: true` — "Use this template", or `gh repo create my-new-project --template
meridun/model-repo`) for bootstrapping a new repo with that config.

## Role and sync workflow

Hub-and-spoke, both directions, one **component** at a time:

```
project A ──(port in: improved component)──▶ model-repo ──(migrate out)──▶ project B, C, …
```

1. A project repo makes a significant improvement to a shareable component.
2. The improvement is ported **into** model-repo, generalised on the way (project specifics →
   `<PLACEHOLDERS>`, project skill prefix → `proj-`).
3. Other project repos migrate the new or updated component **from** model-repo when convenient.

**Nothing is forced downstream.** The inventory is a menu, not a manifest: a project repo adopts
the components it wants, at the granularity it wants (a component's sub-units are listed where
it splits cleanly), and skips the rest. A partial or absent component in a project is a choice,
not drift. The only obligation is the **Depends on** column — take a component's dependencies
with it, or substitute your own and say so in the pin. Projects keep an **adoption record** (a
`## Shared config` table in their README: component → adopted / partial / declined → pin →
reason) so decisions, including opt-outs, are explicit and revisitable.

Sync is manual and ad hoc — no version negotiation. Three things keep it cheap for an agent:

- **Component inventory** (next section). The unit of comparison is a component, never the whole
  repo.
- **Provenance pins.** Each component's L3 doc carries a `> **Upstream pin:**` line. Here it names
  the external upstream, or the project repo and commit the component was last ported from; in a
  project repo it names the model-repo commit last migrated from. Diff from the pin, port, bump the
  pin. Components without an L3 doc pin in the inventory row instead.
- **[`proj-upstream-sync`](.github/skills/proj-upstream-sync/SKILL.md) skill.** The compare/port
  procedure, shipped to project repos so both sides run the same steps. For a downstream pull it
  opens with a short interview (which components, how much of each, adopt/adapt/decline per
  change) so the requester steers the cherry-pick instead of receiving a wholesale migration.
  Every pull also pitches components new since the last one and re-surfaces previously declined
  or partial ones with their recorded reason, so opt-outs stay revisitable as needs change.

For comparison agents: model-repo holds the *generalised* form. A difference that is a project's
placeholder fill-in, prefix rename, or documented local adaptation is **not drift**; only a
substantive improvement (or a bug fix) is a port candidate.

## Component inventory

| Component | Paths here | Depends on | Sub-units (cherry-pickable) | Provenance / pin |
|---|---|---|---|---|
| Doc-tier system (L1/L2/L3) | `.github/copilot-instructions.md`, `CLAUDE.md`, `docs/Documentation.md`, `.github/skills/proj-doc-tiers/`, `.github/skills/proj-agent-skill/` | — | L1 file alone; either skill alone | originated here |
| Config sync + meta-drift guard | `scripts/sync-claude-config.mjs`, `scripts/check-meta-drift.mjs` | Node; the `.github/skills` + `.github/agents` layout | sync script alone (drift guard is optional CI) | ported from a project repo (script header cites its issues #454/#490); **unpinned** |
| Caveman mode hook | `## Caveman mode` in L1 (canonical), `.claude/settings.json` (`UserPromptSubmit`), rule 4 of `scripts/check-meta-drift.mjs`, `test/meta-drift.test.mjs` | Claude Code (hook); L1 alone for Copilot | L1 section alone (no hook) | originated here; no external dependency — see `docs/Development_TokenTools.md` |
| graphify nudge hook | `.claude/hooks/graphify-nudge.py`, `.claude/settings.json` (`PreToolUse`), `docs/Development_TokenTools.md` | Claude Code; graphify installed | doc alone (vtk notes) | originated here; graphify itself is external; vtk notes pinned to [meridun/vtk](https://github.com/meridun/vtk) **fc4b1a5** in `docs/Development_TokenTools.md` |
| Role-based model routing | `.github/agents/*.agent.md` (except `sdlc-worker`), `## Orchestration` in L1, `docs/Development_ModelRouting.md` | config sync (or hand-copy agents to `.claude/agents/`) | any subset of roles; policy section without agents | pilotfish **v1.1.2** — pin in `docs/Development_ModelRouting.md` |
| Agentic SDLC pipeline | `sdlc/` (core `README.md` + `lanes/` + `dispatch.md`; `bindings/gh-issue/` with the reference CLI `sdlc.mjs` and `labels.md`; `PROFILE.md`; `tools/` lint ratchet), `test/sdlc.test.mjs`, `test/check-lint-baseline.test.mjs`, `.github/agents/sdlc-worker.agent.md`, `.github/workflows/ci.yml`, `docs/Development_AgenticSDLC.md`, `docs/Development_Sdlc*.md` | `gh` ≥ 2.86 + GitHub Issues (native dependency edges); Node for the CLI and ratchet only | three layers per `Development_SdlcComposability.md`: normative spec docs only → + core prompts and binding → + reference CLI; individual lanes; the lint ratchet (`sdlc/tools/`) alone. Azure DevOps bindings (`ado-feature`, `ado-pbi`) are **declined** here — take them from upstream | [meridun/agentic-sdlc](https://github.com/meridun/agentic-sdlc) **34b769e** — pin in `docs/Development_AgenticSDLC.md` |
| Upstream sync procedure | `.github/skills/proj-upstream-sync/` | — | — | originated here |

## Bootstrapping a new repo (template use)

Generating a repo from this template gives you every component above:

1. **Documentation tier system (L1/L2/L3)** — cheap-first context loading so agents don't
   re-derive the same architecture facts every session. See
   [docs/Documentation.md](docs/Documentation.md).
2. **Token optimizer tools**:
   - **Caveman mode** — a `UserPromptSubmit` hook that keeps agent replies terse by default
     (`.claude/settings.json`; canonical text in L1, drift-checked, no external dependency).
   - **vtk** — a git/gh/npm output-filtering wrapper (bring your own binary; wiring notes in
     [docs/Development_TokenTools.md](docs/Development_TokenTools.md)).
   - **graphify** — codebase-to-knowledge-graph tool with a `PreToolUse` nudge hook
     (`.claude/hooks/graphify-nudge.py`) that steers agents to `graphify query` before raw
     grep/read once a graph exists.
3. **Role-based model routing** — six repo-committed role agents (`scout`, `Explore`,
   `mech-executor`, `executor`, `verifier`, `security-executor`) pinned to cost tiers via
   frontmatter, plus a role-only orchestration policy in L1. Adapted from
   [pilotfish](https://github.com/Nanako0129/pilotfish) (MIT), moved from global to project
   level so it ships with the repo. See
   [docs/Development_ModelRouting.md](docs/Development_ModelRouting.md).
4. **Agentic SDLC setup** — an issue-driven pipeline (`intake → design → queued → build →
   verify → audit → ship`) shipped as one copyable `sdlc/` tree: core prompts (`README.md`,
   `lanes/`, `dispatch.md`) that are never edited, a `gh-issue` binding carrying the deterministic
   `sdlc` CLI (`sdlc/bindings/gh-issue/sdlc.mjs`) and label taxonomy, a `PROFILE.md` that holds
   every `<KEY>`, and an isolated `sdlc-worker` agent. Fill `sdlc/PROFILE.md` per
   `docs/Development_SdlcAdoption.md` before scheduling it.

### Quick start

1. Generate a repo from this template.
2. Rename the `proj-` skill/agent prefix to your project's own (find/replace across
   `.github/skills/`, `.github/agents/`, `sdlc/`, `.github/copilot-instructions.md`,
   `scripts/check-meta-drift.mjs`).
3. Fill in `docs/Overview.md` and `docs/Architecture.md` with your actual system.
4. `npm install` then `npm run sync:claude-config` to mirror `.github/` into `.claude/`.
5. Delete anything you don't need (the SDLC pipeline in particular is opt-in — it assumes a
   GitHub Issues-driven workflow with `gh` available).

### Layout

```
.github/copilot-instructions.md   L1 — loaded every request (principles + routing)
.github/skills/*/SKILL.md         L2 — loaded on demand (detailed patterns)
docs/*.md                         L3 — loaded explicitly (deep reference)
.github/agents/*.agent.md         Subagent shims (canonical source)
.claude/                          Claude Code mirror, generated — do not hand-edit
sdlc/                             Agentic SDLC tree: core prompts, gh-issue binding + CLI, profile, lint ratchet
scripts/                          sync-claude-config, check-meta-drift
.github/workflows/ci.yml          Tests + config-sync and meta-drift checks on PRs
```

`.claude/skills/` and `.claude/agents/` are generated by `npm run sync:claude-config` from the
`.github/` sources — never hand-edit them.
