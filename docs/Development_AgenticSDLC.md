# The Agentic SDLC model

This is the *why* behind the prompts. Read it once; the prompts are self-contained after that.

> **Upstream pin:** [meridun/agentic-sdlc](https://github.com/meridun/agentic-sdlc) **`34b769e`**
> (2026-09-05). To re-sync, diff upstream's `sdlc/`, `test/`, `agents/sdlc-worker.md`,
> `docs/{AgenticSDLC,Adoption,Composability}.md`, and `docs/profiles/gh-issue.example.md` against
> `sdlc/`, `test/`, `.github/agents/sdlc-worker.agent.md`, `docs/Development_AgenticSDLC.md`, and
> `docs/Development_Sdlc*.md`, then bump this pin. Local adaptations to preserve: doc pointers
> renamed to this repo's `docs/Development_*` / `.github/` paths; `PROD_BRANCH=main` in
> `sdlc/bindings/gh-issue/sdlc.mjs` and the profile; the `proj-doc-tiers` skill name in
> `sdlc/lanes/ship.md`; the `verifier` / `security-executor` stances inlined in
> `sdlc/lanes/verify.md` / `audit.md` (declared in `sdlc/PROFILE.md` § Known deviations); the
> `ado-feature` / `ado-pbi` bindings and their profile examples **declined** (no Azure DevOps
> downstream yet — revisit when one appears); CI in `.github/workflows/ci.yml` targets `dev`/`main`.

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
   not on need. Every stage checks "is my artifact already present for this branch HEAD?" and no-ops if
   so. A re-run must never redo completed work or restart an in-progress branch — it *continues* it.
   The same rule covers **human rewinds**: an item moved back to an earlier stage, or a closed issue
   reopened into `stage:intake`, is reconciled, not re-run from scratch. The stage investigates what
   already exists, trusting artifacts over assertions — merged code / branch state / PR status first,
   then recorded reports for the current HEAD, then issue comments, labels last. Existing valid
   artifacts are presumed good unless the human's rewind comment gives a reason to distrust them or
   the investigation itself finds something significant; then redo exactly the invalidated part, and
   only it. If the evidence shows the work is already fully shipped, any stage may short-circuit:
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
