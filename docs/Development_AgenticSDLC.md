# The Agentic SDLC model

This is the *why* behind the prompts. Read it once; the prompts are self-contained after that.

> **Upstream pin:** [meridun/model-repo](https://github.com/meridun/model-repo) **`21c127c`**
> (2026-09-30). To re-sync, diff model-repo's `sdlc/`, `test/sdlc.test.mjs`,
> `.github/agents/sdlc-worker.agent.md`, `docs/Development_AgenticSDLC.md`, and
> `docs/Development_Sdlc*.md` against the same paths here, then bump this pin and `SPEC_VERSION`
> in `sdlc/PROFILE.md`. Local adaptations to preserve: `PROD_BRANCH=master` in
> `sdlc/bindings/gh-issue/sdlc.mjs` and the profile; CI in `.github/workflows/ci.yml` targets
> `dev`/`master` and runs `pytest` plus `check:docs`; the `verifier` / `security-executor` stances
> inlined in `sdlc/lanes/verify.md` / `audit.md` (declared in `sdlc/PROFILE.md` § Known
> deviations); the `ado-feature` / `ado-pbi` bindings and their profile examples **declined** (no
> Azure DevOps downstream yet — revisit when one appears); the `sdlc/tools/` lint ratchet
> **declined** (`LINT_CMD` unbound; ESLint-only tooling on a Python repo). Seeded originally from
> [meridun/agentic-sdlc](https://github.com/meridun/agentic-sdlc) `34b769e` (2026-09-05, retired).

> **Port record:** prompt refinements, bounce summary, and helper tests ported from
> [meridun/IsekaiOnline](https://github.com/meridun/IsekaiOnline) **78492873f** (2026-09-07).
> Rationale and lessons-learned sections ported from the same source **78492873f** (2026-09-07).
> Prior-work check re-entry rule (upstream-section defects bounce, even over a PARK), candidate
> snapshot field fix, reapable intake docs worktree, focused dispatch (`sdlc/focus.md`,
> `--issue` on `lanes`/`cycle-prep`), and the drift-gated `node-modules` Step 0a refresh ported
> from the same source **122df3187** (2026-09-29).

## The idea

A backlog of GitHub issues, each carrying a `stage:` label, is a state machine. A **coding agent**
advances one issue one stage at a time. A **dispatcher**, on a schedule, spawns one **worker** per
stage. Workers are stateless and isolated — they carry nothing between runs; the **issue thread is the
entire shared memory** of the pipeline. That single constraint is what makes the system robust: any
worker can die mid-pass and the next scheduled run picks up exactly where the issue's labels and
comments say things stand.

```
 raw idea ─▶ intake ─▶ design ─▶ [queued] ─▶ build ─▶ verify ─▶ audit ─▶ ship ─▶ PR ─▶ (human merges) ─▶ closed
             triage    plan the   human       write     prove     review    docs +
             + route   approach   throttle    code      it works  it's safe open PR
```

The **canonical spine** (see [Development_SdlcComposability.md](Development_SdlcComposability.md)) names nine stages:
`intake → design → queued → build → verify → audit → ready → shipping → complete`, with two
human gates (`queued` and `ready`). `design` is a **standard stage** — its spec track (a reviewed
implementation plan in the issue body) always runs; its UX/storyboard track is an optional module
for user-facing forks. The shipped template implements the collapsed tail: `ship`
does docs + PR, the human merge **is** the `ready` gate, and `shipping → complete` collapse into
merge-and-close. Multi-repo forks make the tail explicit; both forms conform.

## The five invariants that make it safe

1. **One issue per pass, one outcome per pass.** A worker CLAIMs one issue, does the stage's work, and
   EMITs exactly one of `ADVANCE` / `BOUNCE` / `PARK` / `CONTINUE` — never silently. No worker ever
   processes two issues in a run. This bounds blast radius and makes every run auditable from the issue
   thread alone.

2. **Idempotency — reconcile against reality, never re-execute blindly.** Schedulers fire on a clock,
   not on need. A re-run must never redo completed work or restart an in-progress branch — it
   *continues* it.
   The same rule covers **human rewinds**: an item moved back to an earlier stage, or a closed issue
   reopened into `stage:intake`, is reconciled, not re-run from scratch. The stage investigates what
   already exists, trusting artifacts over assertions — merged code / branch state / PR status first,
   then recorded reports for the current HEAD, then issue comments, labels last. **Existing work is
   evidence, not proof.** A worker only finds its own stage's artifacts on an item still in its lane
   when something went back: a rewind, a bounce, a crashed or reaped worker, a CONTINUE, an answered
   PARK, or a reopen. Each is a reason to doubt the prior work, so the worker neither skips the stage
   as done nor starts over: it tries to refute what exists, keeps what survives, redoes only what
   fails, and records a short Prior-work check in its emit body. Intake and design derive their own
   answer before re-reading the old one; build re-runs its tests against the acceptance criteria;
   verify, audit, and ship simply run again. A defect found in *another* lane's section bounces to
   the owning lane — even over a PARK — rather than being worked around (see `sdlc/README.md`
   EMIT). If the evidence shows the work is already fully shipped, any stage may short-circuit:
   PARK with the evidence (PR#, commit, observed behavior) for a human to close — no silent
   auto-close, and no pointless ratchet through the remaining lanes. Reality includes work the
   pipeline never produced — a reasonably-named branch on local or origin, work already partly
   merged, artifacts on a predecessor issue linked in the body — so enrolled, cloned, and rewound
   items all reconcile the same way; a branch on neither local nor origin, or unrecognizably
   named, is not discoverable.

3. **Isolation — no delegation, no shared tree.** Workers have no agent-spawning tool (a spawned
   subagent runs detached and strands the item). They never work in the main checkout — each uses an
   issue-scoped git worktree, which doubles as a second lock (git refuses two checkouts of one branch).
   "Owner skill X" in a lane prompt means *apply X's checklist inline*, not *spawn X*.

4. **Stale-lock reaping, never live-lock stomping.** `sdlc:wip` is the lock. The dispatcher reaps a
   lock only when its claim comment is ≥2h old (two full hourly cycles — no legitimate pass runs that
   long), and **verify-before-write**: it re-fetches the newest claim immediately before stripping,
   because its snapshot may be stale under concurrent dispatch runs. A fresh lock is a live worker
   and is left alone; an unprovable age is left alone too. Reaped issues **keep their worktrees** —
   the next worker reuses them (build's CONTINUE resumption depends on it). For a stall it detects
   in its *own* cycle, the dispatcher self-heals by resuming the worker **once**, then parking the
   item `sdlc:needs-human` — never an unbounded retry loop. Human-set labels (`sdlc:needs-human`,
   `sdlc:hold`) are never touched by any automation.

5. **Bounce to the lane that owns the failure; park for the human.** A red test bounces to build; a
   security defect bounces to build; an undecided product question bounces to intake, a spec gap to
   design; a risk
   tradeoff or a "the design itself is wrong" call PARKs to a human via `sdlc:needs-human`. A build
   blocked by a dependency is a **readiness regression**, not a build failure: record the
   dependency as a **native issue-dependency edge** (the item *blocked by* the blocker — the
   edge, not a label or a prose line, is what the dispatcher's eligibility gate reads; the
   `blocked`/`ready` labels are derived from it) and bounce the item to `stage:queued`. The edge
   keeps every lane from claiming the item while the blocker is open, and the human throttle
   gates re-entry when it clears — that gate is what stops a silent queued→build→queued loop.
   Failures flow to accountability, not in a circle. **And every bounce loop is bounded:** if the
   same issue has already been bounced **twice** between the same two lanes for the same class of
   failure (count the lane's prior `sdlc:emit … BOUNCE` comments on the issue), the third pass
   PARKs it `sdlc:needs-human` with the loop history instead of bouncing again — a ping-pong that
   two full round-trips didn't converge needs a human, not a third automated attempt. (This is the
   inter-worker mirror of the dispatcher's resume-once-then-park self-heal.)

**Two corollaries worth naming.** *(a)* The lock has two halves: the `sdlc:wip` label is the
visibility signal, the `sdlc:claim <run-id> <lane>` comment is the ownership record and race
tiebreaker (earliest claim — then lexicographically lower run-id — wins; the loser walks away and
marks its own claim `(superseded)`). The issue-scoped worktree is a third layer: git's
one-checkout-per-branch rule turns a failed `worktree add` into a lost race. Assigning the issue to
a bot identity is cheap belt-and-braces on top. *(b)* Keep the human throttle **manual** while the
pipeline is still shaking out kinks — automating the puller is the last thing to do, if ever.

## Three orthogonal vocabularies — do not confuse them

Most backlogs already carry two axes before this pipeline arrives. This doc adds a third. Keep the
three in distinct label namespaces so a reader never mistakes one for another:

| Vocabulary | Question it answers | Values | Owner |
|---|---|---|---|
| Engineering readiness | _Can this issue be worked yet?_ | ready · blocked · closed — **source of truth: the tracker's native issue-dependency edges** (*blocked by*); any roadmap column and the `ready`/`blocked` labels are mirrors, the labels derived by `sdlc deps --apply` | your backlog/roadmap |
| Design maturity | _How settled is the idea?_ | proposed · building · live | your design pipeline |
| **Queue position** (this doc) | _Which stage's worker should touch it next?_ | `stage:*` labels | this doc |

Queue position is a `stage:` **label namespace** — textual, no emoji — specifically so it never
visually collides with a maturity or readiness axis rendered with icons.

## The label protocol

- `stage:intake` · `stage:design` · `stage:build` · `stage:verify` · `stage:audit` · `stage:ship`
  — the lane an issue is in. **Exactly one per open issue** — the dispatcher's integrity check
  auto-repairs a zero-stage issue to `stage:intake` and parks a multi-stage one (see
  [the gh-issue binding's labels.md](../sdlc/bindings/gh-issue/labels.md)).
- `stage:queued` — **workerless**. The human throttle between design and build: the only gate a human
  must open by hand. What the human reviews there is design's `## Implementation plan` in the issue
  body — approving an *approach*, not just an idea (rejecting a wrong approach at queued costs one
  design pass; rejecting it at audit costs a build+verify cycle). Approve = admit to `stage:build`;
  reject = bounce to `stage:design` with a comment. This is what prevents a runaway pipeline from
  consuming build capacity on half-baked ideas.
- `sdlc:wip` — the per-issue lock. Machine-owned, volatile: workers set/clear it, the reaper may strip
  it. Paired with an `sdlc:claim <run-id> <lane>` comment that records ownership + timestamp.
- `sdlc:needs-human` — parked. A worker hit something only a human can decide. Automation never
  advances or reaps a parked item; it re-enters its lane when the human clears the label.
- `sdlc:hold` — human keep-off. No worker touches it.
- `priority:critical` › `priority:medium` › `priority:future` — CLAIM order within a lane, then FIFO by
  creation date.

Full `gh`-scriptable list: [the gh-issue binding's labels.md](../sdlc/bindings/gh-issue/labels.md).

**Four ineligibility axes, re-evaluated fresh every cycle.** A lane's depth is not its eligible
count: an issue is skipped while it carries a live `sdlc:wip`, `sdlc:needs-human`, or `sdlc:hold`,
**or has any open blocker on a native issue-dependency edge**. The dependency axis is the one
that's easy to leave out — without it a `blocked` item sits in `stage:build` fully claimable, and
matching prose `Depends on` lines by regex instead reads "does not depend on ..." as an edge.
Edges, not labels and not prose, are authoritative: the `blocked`/`ready` labels are *derived*
from edge state (`sdlc deps --apply`, which also lints a label with no edge behind it and any
dependency cycle), `sdlc deps --migrate` converts legacy prose lines into real edges once, and any
prose that stays is a human mirror. Because the gate re-reads live edges each cycle, closing a
blocker unblocks its dependents on the next cycle with no sweep involved. `sdlc lanes` appends a
`(hold N, needs-human N, wip N)` breakdown whenever depth exceeds eligibility, so a snapshot bug
is distinguishable from expected ineligibility without extra queries.

Flags are **orthogonal to the lane**: they gate whether a worker may claim the issue, never which
lane it belongs to. That is what makes `sdlc:hold` useful — you can hand-work an issue without
pulling it out of its queue position. The `sdlc:` prefix marks the control-plane flags as
machine-owned and volatile (`sdlc:hold` and `sdlc:needs-human` excepted: humans own those, and no
automation ever clears them).

## Concurrency variants

The template ships the **per-issue** model. A simpler **serial** model exists — pick by backlog size.

### Per-issue (shipped default)
- Locking is per-issue via claim comments with run-ids + a claim-verify race check; lane workers
  **run concurrently** — each in its own worktree.
- There is **no dispatcher singleton**: any number of dispatch runs — different machines, or
  overlapping scheduled/manual runs on one machine — may execute concurrently. They deconflict via
  three rules: per-issue optimistic claims (the tracker is the shared store), idempotent
  verify-before-write GitHub writes (losing a race is recorded, never an error), and a **per-machine
  filesystem lock** (`.git/sdlc-maint.lock`, 30-min stale reap) that serializes only local
  git/worktree/artifact maintenance and never aborts a cycle.
- A fresh lock only removes *that one issue* from eligibility; it never aborts the cycle.
- **Shared `node_modules` — the junction convention.** A worktree needs the project's dependency
  install to run `<TEST_CMD>` / `<LINT_CMD>`, but a per-tree install is minutes and gigabytes each.
  So the convention is **one root-level `node_modules` junction** (Windows junction — no admin
  needed; a plain directory symlink elsewhere) pointing at the main checkout's install. Both halves
  are exported, tested helpers in the gh-issue binding's `sdlc.mjs`: `linkWorktreeNodeModules` creates it in the
  `worktree` command right after `git worktree add` (idempotent — it never replaces a real
  `node_modules` or a dangling link, and a link failure logs but never fails worktree creation),
  and `unlinkWorktreeRootLinks` removes the link — never its target — before `git worktree
  remove`, because on Windows that command recurses *through* a junction and deletes the target's
  contents. The shared install is the standing hazard: **never run an install inside a worktree**
  — it mutates the main checkout and every other junctioned tree. An issue that changes
  dependencies unlinks the junction (non-recursive — never recursive-delete through it) and
  installs for real. Existing worktrees converge on the convention as they're swept and recreated;
  there is deliberately no repair sweep.
- Best when throughput matters and multiple issues are in flight across lanes.

### Serial (simpler alternative)
- No machine lock, no claim comments, no worktrees required.
- The wip gate is **global**: if *any* issue carries an `sdlc:wip` younger than 2h, the whole run
  **aborts** (a live worker exists somewhere). Older → reap and proceed.
- Lanes run **one at a time**, in pipeline order; workers operate in the main checkout with strict
  tree-hygiene (record and restore the entry branch, never stash human WIP).
- Best when the backlog is small, or when running in a single tree without worktree support.

To switch a shipped pipeline to serial: drop the dispatcher's Step -1 and the claim-comment steps,
replace Step 0's per-issue gate with the global abort-or-reap, and run the per-lane loop serially.

## The design stage — standard, with two tracks

**Every triaged item passes through `stage:design`** (the only bypass is work intake finds already
built, which routes to the earliest absent artifact, floor `stage:verify`). The lane runs two
tracks with deliberately different human seams (`sdlc/lanes/design.md`):

- the **UX track** (an optional module — only for forks that bind `<DESIGN_ARTIFACTS>` in their
  profile, and only when visual/UX design is still owed): build the competing storyboards/mockups,
  then **PARK** for the human's A/B/C pick;
- the **spec track** (every item, after the pick when both apply): write the implementation plan
  into the issue body and **ADVANCE**. Design-exempt work (bug fix, refactor, infra) gets a
  spec-lite — the same headings, a line each — not storyboards.

The two seams differ on purpose — this is the **two-human-seams principle**: *open decisions park
inside the phase that needs the answer; completed artifacts get reviewed at the gate after it.*
The A/B/C pick is a *missing input* with no default — the spec depends on it, so the phase can't
finish without the answer, and PARK begs for attention. The spec is a *completed output* with a
reasonable default (the worker's judgment) — the human's role is veto at the queued gate, where
silence is fine and items batch by capacity.

Product/scope questions stay at intake as **decision debates** — intake PARKs with the options
framed in-issue, the human decides, and intake records a `<DECISION_RECORD>` one-liner before
routing onward. Design owns the UX pick and the plan; intake owns whether/what to build at all.

## Where artifacts live — branches are created lazily

The issue is the spine, but committed artifacts need a branch. A worker creates one **only when it
first has something to commit** — branches are lazy, never pre-allocated — and different stages own
different branches with different lifecycles:

| Stage | Commits? | Branch | Merges to `<DEFAULT_BRANCH>` |
|---|---|---|---|
| intake | no — reads, relabels, edits issue-body sections | — | — |
| design | yes, when the UX track runs — storyboards and design-index edits (the implementation plan itself lives in the issue body, not on a branch) | `docs/<issue>-design` | **fast**, at the design→queued seam |
| build | yes — the implementation | `feat/<issue>-<slug>` (off `<DEFAULT_BRANCH>`) | at ship, via the PR |

**Two branches, not one,** because the two artifact types have different audiences. Design
artifacts are **shared reference** — a design index has to be true for everyone — so they cannot
ride an unmerged feature branch for the weeks a build takes; they merge on their own short-lived
docs branch. The implementation branch is cut afterward, so it already contains the merged design.
Design-exempt items (no design artifacts) simply get their first branch at build. Intake never
branches because it never commits.

**Items built _outside_ the pipeline have no branch — workers fall back to `<DEFAULT_BRANCH>`.**
Work that was implemented and merged by hand before the pipeline ever saw it enters at intake and
routes to the earliest lane whose artifact is genuinely missing (floor `stage:verify`), but there
is no feature branch for the downstream workers to check out. Each carries a **no-branch
fallback**: verify validates against `<DEFAULT_BRANCH>` and names the introducing commit; audit
reconstructs the isolated change diff from that commit, scoped to the issue's files; ship cuts a
fresh branch carrying only the still-missing artifacts (docs fan-out plus any test the earlier
stages left uncommitted) for a docs/tests-only `Closes #<issue>` PR.

## Why the issue thread is the only state

Everything a downstream stage needs, the upstream stage writes onto the issue. **Durable artifacts
live in the body as owned sections** (original author text preserved on top): intake's
`## Requirements` + `## Acceptance criteria`, design's `## Design` + `## Implementation plan` —
baseline `<DEFAULT_BRANCH>` SHA, approach + ordered steps, per-file changes with signatures/shapes/
migrations, risky seams (with an invariant-impact line), test strategy, out of scope. The plan is
**detailed but not code** — it carries every *decision* so build makes only *expression*
decisions; no code bodies or diffs. Plans state their baseline because they rot: build's spec-rot
check re-validates a plan when `<DEFAULT_BRANCH>` has moved over its named paths, bouncing to
design only on material invalidation. **Comments are protocol traffic**: claims, emits, PARK
questions, build's branch name, verify's report and evidence, audit's findings. Workers edit only
their own body sections. A worker reconstructs its entire
context from `gh issue view` + the branch. This is what lets the whole thing survive process death,
run headless on a cron, and be debugged by a human reading one issue top to bottom.

## Manual ↔ scheduled (zero rewrite)

Every stage runs the same loop, and both modes run the **identical** lane prompt body — it doesn't
know or care what fired it. That sameness is the point: a manual paste becomes a cron job with no
rewrite, and a cron job can be debugged by pasting the same file into a session.

- **Scheduled.** [`sdlc/dispatch.md`](../sdlc/dispatch.md) is a thin dispatcher, not an
  orchestrator: per-issue wip gate (reap stale locks only, verify-before-write), machine-locked
  git + worktree maintenance, a stage-label integrity check, then one worker spawned per non-empty
  lane in a single concurrent batch. Each worker reads the universal loop and executes its
  [`sdlc/lanes/`](../sdlc/lanes/) file once in an issue-scoped worktree, ending its reply with the
  fenced JSON result block (`{issue, outcome, next_stage, notes}`) the dispatcher consumes — so
  self-heal and digest read structured data instead of parsing prose, and a missing or malformed
  block is a recorded contract violation with prose fallback.
- **Focused.** [`sdlc/focus.md`](../sdlc/focus.md) runs one ordinary dispatch cycle (same
  maintenance, locks, integrity check, model routing, self-heal, digest) with lane eligibility
  filtered to one issue via `cycle-prep --issue <N>`. Because the dispatcher re-runs a lane
  serially when an ADVANCE lands an item in it — and never runs a lane twice per cycle — the issue
  walks forward lane by lane and stops on PARK, BOUNCE, CONTINUE, or a human gate
  (`stage:queued`, PR merge). Manual and on request only. The filter is deliberately confined to
  the lanes section: a focused cycle still pays for every global ritual, so running one never
  leaves the rest of the pipeline's bookkeeping behind.
- **Manual.** Paste a lane prompt into a session; it does one item, minting its own run-id for the
  claim comment. Claims deconflict per issue, so manual and scheduled runs coexist — which makes
  manual the right mode for exercising an unproven tail before trusting it to the clock.

The **stage-label integrity check** is a hand-edit backstop, not a duplicate of the CLI's
transition validation. The invariant is *exactly one lane label per open issue*: zero makes an
issue invisible to every lane forever (a triage escapee), two makes it eligible in two lanes at
once. The dispatcher counts labels in the snapshot it already holds — no new query — and repairs a
zero-stage issue to `stage:intake` (re-entering at the front for re-routing) or parks a
multi-stage one, leaving its labels untouched because a snapshot can't adjudicate the right stage.
The CLI never creates either state; labels edited by hand or by outside tooling still can, so the
runtime check stays.

## Why there's a CLI — and what each one-shot buys

The recurring part of the worker loop is **not** judgment — it is the same label-swap plus git
context dance every pass, and hand-typing `stage:verfy` silently corrupts a lane.
[`sdlc`](../sdlc/bindings/gh-issue/sdlc.mjs) collapses that ritual into named one-shots; the agent
still writes every comment and report **body**, and the CLI only does the mechanical label and
branch math. Two design rules carry the safety load: each command's core is a **pure function**
exported and unit-tested with the tracker/git executors injected, so dispatch logic is verified
with no side effects; and the core **throws rather than guesses** — an issue with zero, multiple,
or unknown lane labels routes to a human instead of being interpreted.

- **`advance` / `emit` — the typo-killer.** Every requested transition is validated against a
  hard-coded stage graph (the forward lanes plus the documented bounces), and an illegal jump or a
  misspelled target exits non-zero with *no* mutation. `sdlc:wip` is removed only when actually
  present, and removes precede adds so a lane is never momentarily label-less.
- **`emit` — the completion signal.** It is a worker's only legal way to finish an item: one
  machine-parseable marker comment plus the outcome's label math, atomically. The claim boundary
  is the newest `sdlc:wip` *unlabeled* timeline event (with the emit marker as fallback) rather
  than a regex over prose — a prose-only outcome comment settles nothing, which is the phantom-lock
  bug: a finished worker's claim looks live forever, every later claimer falsely loses the race and
  walks away leaving the lock it just added, until the stale reap. `emit` also refuses to run when
  the caller's run-id doesn't own the live claim.
- **`claim --next` — the pick rule lives in code.** Workers stop eyeballing priority/FIFO order:
  the CLI computes the next eligible issue, claims it atomically, retries the next one on a lost
  race, and exits `idle` on an empty lane.
- **`cycle-prep` — one delimited report per cycle.** The pre-dispatch sequence
  (mint → maint-lock → lanes → gate --reap → deps → sweep → git-maint → node-modules →
  worktree-sweep → conflict-scan → maint-release) is fixed and zero-judgment, so it collapses into one command
  emitting one machine-readable report; the dispatcher then spends its round-trips on the only
  real decision, which workers to spawn. It is a **composer, not a reimplementation** — each
  section literally invokes the standalone subcommand, so their invariants and tests carry over
  and every command stays independently callable. The maintenance sections run only while this run
  holds the maintenance lock and releases it in a `finally`; a lock held elsewhere is *reported and
  skipped*, never a cycle failure. It cannot run from inside a worktree (the lock is a directory
  under a real `.git`), which is why the dispatcher only ever runs it in the main checkout.
  `--issue <N>` (and `lanes --issue <N>`) is the focused-dispatch filter: depths, ineligible
  breakdowns, and the integrity list stay global; only the eligible/blocked lists are trimmed to
  #N, plus one verdict line — `focus #N: eligible in <lane>` | `ineligible in <lane> (<reasons>)`
  | `queued (human gate)` | `no stage label` | `multiple stage labels (…)` |
  `unknown stage label (…)` | `not open (closed or nonexistent)`.
- **`node-modules` — keep a shared install honest, drift-gated.** Hosts that share one install
  across worktrees (the `worktree` command junctions the main checkout's `node_modules`) otherwise
  run the OLD dependency tree after a dependency-changing merge until some worker triages a
  version-shaped red test — and a security bump stays un-installed in the code actually
  executing. The probe is the sha256 of the main checkout's `package-lock.json` against
  `node_modules/.sdlc-install-stamp` (inside `node_modules`, so a wipe invalidates it and it is
  never committed): equal is a no-op; drift runs a plain `npm install` in the main checkout,
  resolved from `git rev-parse --git-common-dir` so the command is worktree-safe. Never `npm ci`
  (its recursive delete next to junctioned worktrees can empty the shared install), skipped while
  `package.json`/`package-lock.json` are dirty in the main checkout (human dependency WIP), and a
  failed install is one greppable `node-modules: install FAILED (…)` line, never a failed cycle.
  It sits after `git-maint` (which is what brings a merged lockfile into the main tree) and before
  `worktree-sweep`.
- **`maint-lock` / `maint-release` — serialize the local half only.** Tracker writes are
  idempotent and deconflict themselves; the filesystem does not. The per-machine lock covers
  git/worktree/artifact maintenance and nothing else, and *held* means skip, never abort.
- **`worktree-sweep` — reap what is provably done.** A tree is removed only when it is **clean**
  *and* **done** — branch gone, ancestry-merged, PR merged, or issue closed. Positive "landed"
  signals only, so a fresh unpushed branch is never swept; dirty or still-active trees are left and
  reported; an unreadable tree is treated as dirty and fails closed. The sibling-name pattern
  (`<WORKTREE_ROOT>/<issue#>`, digit-only suffix) is the destructive path's safety boundary — the
  main checkout and human worktrees can never match it. Worktree creation links the shared
  `node_modules` junction and the sweep unlinks it *before* removal (see
  [Concurrency variants](#per-issue-shipped-default) for why that ordering is load-bearing).
- **Stray tolerance.** A single 0-byte untracked file — a mangled result line pasted into a shell
  and normalized into a `>` redirect — used to make an otherwise-reapable tree read dirty forever.
  A tree now counts as "clean modulo strays" only when *every* status entry is untracked *and*
  every such path is exactly 0 bytes; any tracked change, any untracked file with content, a
  quoted special-character path, or an unreadable path stays dirty. Vetted strays are deleted
  non-recursively (never a directory, which would fail closed) on the apply path only.
- **`conflict-scan` — nudge once per integration-branch advance.** For each open PR that conflicts
  with `<DEFAULT_BRANCH>`, resolve the linked issue, skip locked/parked/held/closed ones, and
  either comment or comment-and-bounce to `stage:build` (conflict resolution is build's lane).
  Idempotency is a **watermark compare**, not a dedup set: the last conflict comment's timestamp is
  checked against the integration branch's tip commit date, so a stuck conflict is re-nudged once
  per advance rather than every cycle, and a resolved-then-reopened conflict is still flagged. The
  scan never merges, updates, or closes a PR — that stays human-gated, mirroring the queued
  throttle and the human merge at the tail.
- **`sweep` — read and ack are separate commands.** Reporting the "these closes unblocked these
  issues" work-list mutates nothing, so a dispatcher or a human can peek without consuming it; the
  bounded swept marker is written only by `sweep --ack`, which intake runs *after* completing the
  unblock edits. A worker that dies in between re-lists the same closes next pass — at-least-once
  delivery paired with idempotent edits.
- **`dup-check` — a deterministic scorer instead of eyeballing a list.** Title hits weigh 3, body
  hits 1, a label-token hit +1; ranked descending, ties by issue number ascending. The exit code is
  the consumer contract (**0** clean, **2** candidates found, **1** usage error) so callers branch
  on it without parsing prose. The free-text query never reaches a shell — it feeds only the pure
  scorer, while the issue listing uses a fixed argv with no query interpolation. Worth copying for
  any future search subcommand.
- **`deps` / `deps --migrate` — the dependency axis, kept in edges.** See the ineligibility axes
  under [the label protocol](#the-label-protocol): labels are derived, prose is a mirror, edges are
  the truth.

## Lessons from production runs

Each line is an observed failure and the rule it produced.

- A worker spawned a subagent to do its stage → the subagent ran detached and stranded the item:
  workers get no agent-spawning tool; "owner skill X" means apply X's checklist inline.
- Lock age was read from the issue's `updatedAt` → an unrelated comment made a stale lock look
  fresh: age comes from the `sdlc:wip` `labeled` timeline event, nothing else.
- A finished worker's prose outcome comment left its claim looking live → every later claimer lost
  a phantom race: the claim boundary is the `sdlc:wip` unlabeled event, and `emit` is the only
  legal finish.
- A worker hand-picked the "next" issue in its lane and took the wrong one → the pick rule moved
  into `claim --next`.
- A worktree sweep recursed *through* a `node_modules` junction and emptied the shared install:
  unlink the junction before `git worktree remove`, and never install inside a worktree.
- A 0-byte stray file kept a finished worktree "dirty" forever → strays are classified, not
  guessed at.
- Blocking lived in prose and labels → the eligibility gate ignored it and a regex read "does not
  depend on ..." as an edge: native dependency edges became the source of truth.
- A conflicted PR was re-nudged every hourly cycle → watermark the nudge against the integration
  branch's tip.
- A dispatcher singleton serialized whole cycles → an overrunning cycle aborted the next one
  wholesale: per-issue claims plus a per-machine maintenance lock replaced it.
- The dispatcher parsed issue numbers out of freeform worker replies and missed some → workers end
  with a fenced JSON result block, and lane-level self-heal auto-discovers stalled items.
- An item ping-ponged between two lanes indefinitely → the third bounce of the same class parks
  for a human.
- Hand-edited labels produced zero-stage and dual-stage issues the CLI could never create → the
  dispatcher's integrity check stays as a runtime backstop.

What the chain runs themselves proved: one item ran the full spine end to end (every worker's
ADVANCE path, plus PARK from design and from verify), and one already-built item proved the
outside-pipeline entry and the downstream no-branch fallbacks. Most BOUNCE tails and build's
CONTINUE stay thinly exercised — run those by hand before trusting them to the clock. The lanes,
not this list, are canonical for behavior: every lesson above is already folded into them.
