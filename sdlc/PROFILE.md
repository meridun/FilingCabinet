# SDLC profile: <PROJECT>

**The one file adoption fills.** Every `<KEY>` a prompt in this tree names resolves to a row of
§ Keys; the binding chosen here says how every abstract operation is performed; the variation
points and deviations make drift audits against `docs/Development_SdlcComposability.md` mechanical. Worked
examples: `docs/Development_SdlcProfileExample.md` in the framework repo.

Spec version: `<SPEC_VERSION>` — the framework tag this profile was written against.

## Keys

Required:

| Key | Value | Meaning |
|---|---|---|
| `BINDING` | `gh-issue` | which `bindings/<name>/BINDING.md` is bound (`gh-issue` here; `ado-feature` · `ado-pbi` live upstream in agentic-sdlc and are not carried in this repo) |
| `SDLC_CLI` | `node sdlc/bindings/gh-issue/sdlc.mjs` | the binding's deterministic core invocation, or `none` (`sdlc …` in any prompt means this) |
| `SPEC_VERSION` | `34b769e` | framework git tag (or sha) this profile tracks — see the pin in `docs/Development_AgenticSDLC.md` |
| `PROJECT` | | project name (every worker's opening line) |
| `REPO_PATH` | | local working directory the scheduled agent runs in |
| `WORKER_AGENT` | `sdlc-worker` | the isolated worker agent's `subagent_type` name — `.github/agents/sdlc-worker.agent.md` here; rename project-scoped (e.g. `acme-sdlc-worker`) on adoption |
| `DEFAULT_BRANCH` | `dev` | integration branch — PRs target it, branches cut from it |
| `PROD_BRANCH` | `main` | release branch — **off-limits** to all workers |
| `WORKTREE_ROOT` | | where issue-scoped worktrees live (e.g. `../acme-wt`) |
| `BUILD_CMD` | | build/compile/launch the software for a real run |
| `TEST_CMD` | | targeted test run (build stage) |
| `FULL_SUITE_CMD` | | full test suite (verify stage) |
| `LINT_CMD` | | lint/format/type gate; on a repo with a lint backlog bind the ratchet `node sdlc/tools/check-lint-baseline.mjs` |
| `SMOKE_CMD` | | repeatable real-run / e2e / smoke spec (verify stage) |
| `LANG_CONVENTIONS` | | the lint/format/test bar in one line |
| `INVARIANTS` | | project rules that are ACs on **every** change — list them; the key that most repays effort |
| `DECISION_RECORD` | | where decisions are logged (a doc section, a registry file) |
| `DOCS_SINKS` | | documentation targets ship fans out to |

Optional — an unbound key means the lane step it gates is **skipped, not improvised**:

| Key | Value | Meaning |
|---|---|---|
| `DESIGN_ARTIFACTS` | | design UX track: the project's conventions for storyboards/mockups — what they are, where they live, how they're authored; unbound → spec track only |
| `KNOWN_ENV_LIMITS` | | verify: a gate that can't run here, its accepted substitute, and the report wording |
| `DEP_AUDIT_CMD` | | dependency-vulnerability scanner (intake's per-pass sweep; audit's lockfile-diff check) |
| `MIGRATIONS_DIR` | | schema-migrations directory (verify migration checks; audit diff-shape check) |
| `MIGRATE_DOWN_CMD` / `MIGRATE_UP_CMD` | | with `MIGRATIONS_DIR`: roll the newest migration back / forward on a disposable DB |
| `SCHEMA_DUMP` | | with `MIGRATIONS_DIR`: the committed schema dump the migration tool regenerates |
| `DOCS_ROOT` | | docs-tiers skill: root of the L3 documentation tree |
| `DOC_DOMAINS` | | docs-tiers skill: thematic domain prefixes |
| `TOKEN_TOOL` | | shell-output compactor and its mode (explicit-prefix / transparent-wrapper — see `.github/agents/sdlc-worker.agent.md`) |

## Variation points (`docs/Development_SdlcComposability.md`)

- **Spine:** `intake → design → queued → build → verify → audit → ship` with the collapsed tail
  *(or the explicit `ready → shipping → complete` tail)*.
- **VP1 tracker:** per `bindings/<BINDING>/BINDING.md` — note here anything bound differently.
- **VP2 topology:** single repo *(or multi-repo Feature/child — routing markers: …)*.
- **VP3 modules:** design UX track *(off | bound — trigger, conventions)*; PSI lane *(off | on)*.
- **VP4 dispatcher:** trigger *(scheduled task / cron / CI / interactive)*, machine-lock
  representation *(`.git/sdlc-maint.lock` mkdir + 30-min rename reap)*, worker isolation
  *(issue-scoped worktrees at `<WORKTREE_ROOT>/<issue#>`)*, per-lane model tiers if they differ
  from `dispatch.md`'s table.
- **VP5 quality bars:** the commands above; which lint gate form (`clean` vs ratchet); known env
  limits; per-repo bars if multi-repo.
- **Deterministic core:** `<SDLC_CLI>` and which operations it owns *(or `none`)*.

## Known deviations from spec

List each with why. Undeclared divergence is drift; declared divergence is legitimate.

- `lanes/verify.md` and `lanes/audit.md` each carry one added paragraph telling the worker to apply
  the `verifier` / `security-executor` role stance inline (never spawn it). Why: this repo ships
  those role agents (`docs/Development_ModelRouting.md`); the stance is the lane's quality bar.
- `lanes/ship.md` names the `proj-doc-tiers` skill (upstream: `documentation-tiers`). Why: skill
  prefix convention of this repo.
- Doc pointers in the core and binding files point at `docs/Development_*` and `.github/` paths
  instead of upstream's `docs/*.md` / `agents/` / `skills/`. Why: this repo's doc-tier layout.
- `bindings/ado-feature` and `bindings/ado-pbi` are not carried. Why: no Azure DevOps downstream
  yet; copy them from upstream when one appears.
