# Cursor Multi-Agent PR Orchestrator — Architecture Plan

> Implementation lives in `src/cursor_orchestrator/`, installable as the
> global `cursor-orchestrator` command — see `README.md` for install and
> usage. This file stays the architecture doc; the code is what actually
> runs.

## Goal

A Python orchestrator that takes a single natural-language prompt, plans the
work in Cursor as a small, explicit set of sub-agent roles, gets human
sign-off on scope (looping on edits until approved), then executes against
**one feature branch** — sequencing or parallelizing subtasks per the plan's
own dependency graph. Each subtask is driven through an Implementer ↔ Tester
↔ Reviewer loop using the **same original prompt** for review, until the
reviewer is satisfied or a **deterministic iteration cap** forces a human
check-in. Once accepted, it opens a PR — never auto-merges — and then keeps
**babysitting** the PR: watching CI and human review comments, routing fixes
back through the implementer, until the PR is green and addressed. A human
is always the final approver and the only one who merges.

## Sequential campaigns: an entire architecture, not just one prompt

Everything in Flow below is what happens for **one** task/prompt, ending
in one PR. `campaign.py` adds an optional tier *above* that flow, for
when the input is an entire architecture rather than one scoped change:

```
architecture description
  │
  ▼
[0a] FEATURE PLANNER (same model/role as the planner, coarser grained)
  → FeaturePlan { summary, features[] }
  each feature: id, description, tasks[] (id, description, depends_on[])
  each task is sized so its eventual PR stays reviewable by one person —
  it becomes exactly one branch and one PR, built via the normal
  Flow below, unmodified
  │
  ▼
[0b] HUMAN FEATURE-PLAN APPROVAL ← blocking gate (approve / edit / abort)
  once per whole architecture, not per task
  │
  ▼
for each task, in dependency order (ties broken by declared order):
  [0c] sync the local base branch to origin (fetch + fast-forward-only;
       a non-fast-forward is a hard stop, same policy as every other
       merge in this system)
  [0d] run this ONE task's description through the normal Flow ([1]-[7]
       below), unmodified — its own plan, its own plan critique, its own
       human scope-approval gate, its own build/review/PR/babysit
  [0e] once babysit reaches "clean" (CI green, no open comments), WAIT —
       poll until a human actually merges the PR (not just until CI is
       clean, which is as far as a normal single-task run waits).
       Unbounded by design (see risk notes) but interruptible: progress
       is persisted to a state file after every step, so killing the
       process and re-running the same command resumes instead of
       re-planning or rebuilding already-merged tasks.
  [0f] on the build reaching an aborted/took-over/escalated state, or the
       PR being closed without merging: halt the whole campaign and print
       which task needs a human, rather than guessing how to proceed
```

A normal single-prompt run (no `--sequential`) never touches any of this
— `campaign.py` is purely additive.

## Explicit non-goals

- No auto-merge. Ever. The reviewer's verdict gates the *revision loop*, not
  the merge. Babysitting the PR gates the *fix loop*, not the merge either —
  the orchestrator can push fix commits, it never clicks merge.
- Not a general agent framework — this is scoped to one workflow
  (prompt → plan → build → review → PR → babysit).
- Not trying to make cross-model "agreement" a correctness proof. It's a
  second opinion attached to the PR for the human to weigh.
- Not an unbounded agent swarm. The orchestrator starts a small, fixed
  number of agents per group and stays there — fan-out is capped and
  visible, not something the planner can silently scale up.
- Babysitting is not unattended forever — it runs under its own bounded
  cap (iterations *and* wall clock) and escalates to the human instead of
  guessing when it doesn't know what to do (e.g. an ambiguous review
  comment, a flaky-looking CI failure).

---

## Flow

```
prompt
  │
  ▼
[1] PLANNER (Cursor agent, plan mode)
  → Plan { summary, subtasks[] }
  each subtask: id, description, files_likely_touched, depends_on[]
  builds the dependency graph → decides which subtasks form a
  sequential chain vs. an independent parallel group
  │
  ▼
[1b] PLAN CRITIQUE (different model than the planner)
  → PlanCritique { findings[] (severity-labeled) }
  an independent second opinion on the decomposition itself, before any
  code exists: subtask sizing, missing/unnecessary depends_on edges,
  prompt coverage, scope creep. Purely advisory -- no verdict, never
  auto-blocks anything (the Reviewer role is the system's one and only
  verdict authority; a second thing that looks like a gate here would
  reintroduce the same "who wins" ambiguity that design avoids). Shown to
  the human alongside the plan at the next step.
  │
  ▼
[2] HUMAN SCOPE APPROVAL  ← blocking gate (approve / edit / abort)
  - human sees the plan AND the plan critique together
  - "edit" loops back into the planner with the human's corrections,
    re-presented (re-critiqued too) until explicitly approved — this is
    not a one-shot ask
  │
  ▼
[3] BRANCH SETUP
  - one feature branch, created off base — this is the ONLY branch that
    ever becomes the PR; nothing else survives past this run
  │
  ▼
[4] EXECUTE PLAN, one dependency-ordered group at a time
  for each group (subtasks within a group run in parallel; groups
  themselves run sequentially):
    - if the group contains a stub/interface subtask, it runs first,
      alone, and merges to the feature branch before any implementer
      that depends on its contract starts — a stub is not "parallel
      with" the things that depend on it
    - implementer agent(s): each subtask gets its own worktree off the
      feature branch's current HEAD (not its own long-lived branch)
    - tester agent: writes/runs tests per subtask
        - against a stub, this only checks contract/shape conformance —
          it is NOT a substitute for testing real behavior and must be
          re-run once the real implementation lands
        - against the real implementation, full edge-case/failure-path
          testing
    - on group completion: merge each worktree back into the single
      feature branch, sequentially. A merge conflict is a hard stop
      (escalate), never silently resolved. Next group starts from the
      updated HEAD.
  │
  ▼
[5] REVIEW (different model, SAME original prompt + current feature-branch
    diff + rationale)
  checklist per subtask:
    - requirement match (re-read original prompt cold, compare to diff)
    - correctness (independent trace, not just diff skim)
    - test quality (edge cases / failure paths, not just presence)
    - standard-library / codebase-convention compliance
    - security / perf red flags
  output: ReviewResult { verdict: ready_for_pr | needs_revision,
                          findings[] (severity-labeled),
                          revision_requests{ subtask_id -> instructions } }
  │
  ├─ needs_revision ──► route revision_requests to the owning
  │                     implementer/tester, re-run, return to [5]
  │                     (iteration count increments)
  │
  ├─ iteration count hits N (default 3), still not ready_for_pr:
  │     [5b] HUMAN GATE — blocking, deterministic, not a judgment call
  │     the model gets to skip. Human sees outstanding findings + diff
  │     and chooses: allow M more iterations / take over manually /
  │     accept as-is and proceed to PR flagged. The orchestrator does
  │     not decide this on its own.
  │
  ▼ ready_for_pr
[6] CREATE PR (using template)
  - fills: original prompt, plan summary, per-subtask rationale,
    reviewer's findings (labeled "second opinion — verify"), test results
  - opens PR from the single feature branch against base — does NOT
    approve or merge
  │
  ▼
[7] BABYSIT PR  ← polling loop, runs until terminal state or human takes over
  - poll whether the base branch has moved since the feature branch was
    created (local ref only — see risk notes on remote drift)
  - on base-branch drift: merge it into the feature branch immediately
    (never rebase — see risk notes on why), then re-run the Tester agent
    locally before pushing
      - merge conflict → hard stop, escalate, never auto-resolve (same
        policy as the group-boundary merges in step [4])
      - merge succeeds, tests still pass → push the merge commit directly
      - merge succeeds, tests now fail → route to the implementer as a fix,
        same as a CI failure below, then push
  - poll CI status on the feature branch
  - poll human review comments / change-requests on the PR
  - on CI failure:
      pull failure logs → route to the owning implementer → re-implement
      → push fix commit → back to top of [7]
  - on human review comment / requested changes:
      treat as a revision_request → route to the owning implementer →
      push fix commit → back to top of [7]
  - on ambiguous comment / repeated failure of the same check /
    iteration or time cap hit → stop polling, escalate to human (comment
    on the PR or notify via CLI), do NOT keep guessing
  - before pushing any fix commit: check the branch HEAD SHA hasn't moved
    since last read — if a human pushed manually in the meantime, back
    off instead of overwriting
  - terminal states: CI green + no open change-requests (idle, awaiting
    human merge), PR closed/merged by human, or escalated to human
  - never: approve its own PR, dismiss a review, merge, force-push over
    a human's manual commit to the branch
  │
  ▼
  human reviews, resolves any escalation, and merges manually
```

---

## Component design

### `models.py`
Dataclasses: `TaskStatus` (enum), `SubTask`, `Plan` (with
`parallel_groups()` — topological batching; raises on an unknown
dependency, a dependency cycle, *and* on duplicate subtask ids from the
planner, since model-generated output isn't guaranteed unique and a
silent dict-comprehension collision would drop a subtask from execution
without anyone noticing), `TestResult`, `ReviewFinding`, `ReviewResult`,
`PlanCritiqueFinding`, `PlanCritique` (advisory-only, no verdict field —
see step [1b] in Flow).

One tier above that: `Task`, `Feature`, `FeaturePlan` (with
`ordered_tasks()` — same duplicate-id/unknown-dependency/cycle validation
as `Plan.parallel_groups()`, but flattens to a single strict sequential
order instead of parallel batches, since `campaign.py` always builds one
task at a time). A `Task` here is a different, coarser thing than a
`SubTask` — it becomes one branch and one PR, and is itself handed to
the normal `Plan`/`SubTask` machinery once its turn comes up.

### `prompts.py`
- **Planner system prompt** — instructs decomposition into subtasks with
  explicit `depends_on` edges. Dependency ordering alone determines
  execution grouping; there is no separate stub/interface subtask kind.
- **Plan critic system prompt** — instructs an independent critique of the
  decomposition itself (subtask sizing, missing/unnecessary dependency
  edges, prompt coverage, scope creep), severity-labeled findings, no
  verdict.
- **Tester system prompt** — instructed to write tests for edge cases and
  failure paths, not just the happy path. It only writes the tests; it is
  explicitly told it does not need to report whether they pass, because
  that's independently verified by actually running `testing.command`
  (see `cursor_cli_client.py`), not trusted from the agent's own
  self-report.
- **Reviewer system prompt** — the checklist above, instructed to review
  the diff *blind first* (before seeing implementer rationale) to avoid
  anchoring, then reconcile. Forced to output severity-labeled findings,
  never a bare "approved."
- **Feature planner system prompt** — decomposes an entire architecture
  description into features and tasks, explicitly sized so a single
  task's eventual PR "stays reviewable by one person in one sitting" —
  the same framing used to explain "substantial" to the human who asked
  for this. Same model/role as the planner (`config.models.planner`),
  just a coarser-grained pass; not a separately configured model.

### `clients/base.py`
Abstract interfaces: `CursorClientBase.plan()`, `.implement_subtask()`,
`.create_pr()`, `.get_pr_status()`, `.get_pr_review_comments()`,
`.push_fix_commit()`, `.get_pr_merge_state()`; `TestClientBase
.write_and_run_tests()`; `ReviewerClientBase.review()`;
`PlanCriticClientBase.critique()`; `FeaturePlannerClientBase
.plan_features()`. Everything else in the system talks to these
interfaces, not to a specific SDK — swap implementations without
touching orchestrator logic.

`get_pr_merge_state()` returns `"open" | "merged" | "closed"` — used
only by `campaign.py`'s merge-wait, since nothing else needs to know
whether a PR has actually been merged (a normal single-task run stops
once CI is clean; it never asks this).

### `branch_manager.py`
Owns the single feature branch's lifecycle: creates it, opens/tears down
a worktree per subtask in the active group, merges each worktree back
into the feature branch sequentially once its group completes, and hard
stops (no auto-resolve) on merge conflict. This is the piece that makes
"one branch, dependency-ordered groups" real instead of aspirational —
without it, "parallel worktrees merging into one branch" is just prose.
A subtask redone after a revision request reuses its worktree branch
name, so `create_worktree` force-resets it (`git worktree add -B`, not
`-b`) rather than failing on "branch already exists" — `remove_worktree`
only removes the worktree, not the branch itself.

Also exports a module-level `sync_base_branch(repo_path, base_branch)`
(not a `BranchManager` method — no worktree-root tempdir needed just to
sync a branch), used only by `campaign.py` between sequential tasks:
fetch + fast-forward-only merge, hard stop (never a 3-way auto-merge) if
local history has diverged unexpectedly.

### `clients/cursor_cli_client.py`
Real implementation via subprocess calls to the `cursor-agent` CLI — plan,
critique, implement, test, *and* review are all `cursor-agent`
invocations, each constructed with that role's configured `--model`. Both
the reviewer and the plan critic are just another `cursor-agent` call
pointed at a different model than the role they're independently
checking (`config.py` enforces `models.reviewer != models.implementer`
and `models.plan_critic != models.planner` at load time, so "independent
second opinion" is structural, not convention). PR lifecycle and CI
status go through the `gh` CLI by default, since that's a
hosting-provider concern, not an agent concern.

Every `cursor-agent`/`gh` subprocess call has a hard timeout
(`limits.cursor_agent_timeout_seconds` / `limits.gh_timeout_seconds`) —
without one, a single hung process would hang the whole orchestrator
forever, including mid-`babysit()`; this is a *per-call* cap, distinct
from the iteration/wall-clock caps that bound the surrounding loops. The
task/prompt text is passed via stdin rather than a trailing positional
arg, to avoid `ARG_MAX`/escaping problems once a review's diffs get
large. JSON parsed from a CLI's stdout is wrapped so a malformed or
non-JSON response raises a clear error with the raw output attached,
instead of a bare `JSONDecodeError`.

**Verify against your installed `cursor-agent --help` / current Cursor
docs before relying on this** — exact flags, the stdin-input assumption,
and the API surface have been changing release to release; treat the
flags in this file as a starting point, not gospel.

### `clients/github_api_client.py`
Drop-in alternative to `gh` for the PR-lifecycle methods only
(`create_pr`, `get_pr_status`, `get_pr_review_comments`) — for machines
where the `gh` CLI itself isn't allowed (org policy, locked-down build
box, etc.) but direct HTTPS to GitHub's REST API is fine. Selected via
`config.yaml`'s `git.pr_backend: "api"` (default is `"gh"`). It wraps a
`CursorCliClient` by composition and delegates `plan`/`implement_subtask`/
`get_branch_head_sha`/`push_fix_commit` straight through unchanged (the
last two are already plain `git` subprocess calls with no `gh`
involved) — only the actual GitHub-hosting operations are reimplemented,
against the REST API, using stdlib `urllib` (no new dependency). Needs a
`GITHUB_TOKEN` (or `GH_TOKEN`) env var; owner/repo and the API host
(`api.github.com` vs. a GitHub Enterprise `/api/v3` path) are
auto-detected from the repo's `origin` remote, not configured by hand.
Reads CI state from the Checks API only — if your CI posts exclusively to
the older classic commit-status API instead of GitHub Checks, this won't
see it.

### `clients/mock_clients.py`
Fully working fakes with no network calls — used to unit-test the
orchestration/state-machine logic (dependency batching, plan critique,
approval gate, revision loop, iteration cap, PR payload construction)
independent of any live API.

### `orchestrator.py`
The state machine described in Flow above. Owns the group-by-group
execution, the plan-critique call at [1b] and human-approval blocking
point at [2] (critique is re-run against every re-plan produced by an
"edit" decision, not just the first draft), the deterministic iteration
cap and human-gate hand-off at [5b]. This is the part that's actually
unit-testable without your credentials, and the part I ran `pytest`
against before handing this over.

### `babysitter.py`
Owns step [7]. A polling loop (own iteration *and* wall-clock cap,
separate from the implementer↔tester↔reviewer cap in step [5]) that
watches CI status and human review comments on the opened PR, and
re-enters the implementer for a fix — never a full re-plan. There's only
one branch/PR (see branch_manager.py), so a fix isn't attributed back to
a specific original `SubTask`; the implementer is just asked to fix the
branch generically, with the original prompt as context.

It also watches the base branch: baseline is the base branch's SHA at the
moment the feature branch was created (`BranchManager.
base_branch_sha_at_creation`), not "whatever it is when babysitting
starts" — so drift picked up on the very first poll also covers anything
that landed on the base branch during the build/review phase, not only
drift that happens while actively babysitting. On drift, it merges the
base branch into the feature branch (never rebases — see risk notes),
hard-stops and escalates on conflict exactly like the group-boundary
merges in step [4], and otherwise re-runs the Tester agent on the merge
result before pushing, so a "clean" base branch merge can't silently
reintroduce a break that no individual subtask's tests would have caught.

Escalates to the human (PR comment + CLI notification) on ambiguous
input, a repeated failure of the same check, cap exhaustion, or a base
merge conflict, rather than retrying or auto-resolving blindly.

### `pr_template.md`
Filled at step [6]: prompt, plan, per-subtask rationale, reviewer findings
(explicitly labeled as a second opinion to verify, not fact), test results.

### `campaign.py`
Owns the sequential-campaign tier described above. `CampaignState` is a
small JSON-serializable record (the architecture prompt, the approved
`FeaturePlan`, and each task's status/PR URL/branch) — `CampaignRunner`
loads it if the state file already exists (resuming: skip feature
planning and every already-`merged` task) or creates it fresh after the
human approves the feature plan. For each remaining task it builds a
plain `orchestrator.py` `Orchestrator` (identical role clients every
time) scoped to just that task's description, then, once that task's PR
is CI-clean, polls `get_pr_merge_state()` until a human actually merges
it. Halts the whole campaign — rather than guessing how to recover — on
an aborted/taken-over/escalated task, a PR closed without merging, or a
`KeyboardInterrupt` during the merge-wait (printing a clear "re-run to
resume" message in the last case, since state is persisted after every
step). Adds nothing to the plan/build/review/PR logic itself; a normal
single-prompt run never imports this module.

### `cli.py`
Entry point: `python cli.py "build a feature that..."` — wires real clients
together, or `--dry-run` to use the mocks and walk the flow without
touching Cursor at all. The prompt can come from a `--prompt-file` (read
and stripped) instead of the positional argument — the two are mutually
exclusive, exactly one required. `--sequential` switches from a single
`Orchestrator` run to a `CampaignRunner` run over the same role clients;
`--state-file` overrides where that campaign's progress is persisted
(default: derived from the repo path and prompt, under
`~/.cursor-orchestrator/campaigns/`, deliberately outside the target
repo so nothing needs gitignoring).

---

## Honest risk notes

- **Cursor CLI/API surface is a moving target** — the integration points in
  `cursor_cli_client.py` need a check against current docs before first
  real run; don't trust the flags as-written.
- **Reviewer findings are a second opinion, not ground truth** — the PR
  template labels them as such on purpose. Don't let that framing erode
  over time.
- **Plan critique is advisory, not a gate — even a "blocking"-severity
  finding doesn't stop anything on its own.** The human at the
  scope-approval gate can approve the plan anyway; nothing in the code
  forces them to read or act on the critique before typing "a". That's a
  deliberate tradeoff (see step [1b] in Flow — a second automatic gate
  here would reintroduce the "who wins" ambiguity the single-verdict-
  authority design was built to avoid), but it means the critique is only
  as useful as the human actually reading it.
- **Single feature branch doesn't eliminate merge conflicts, it just moves
  and multiplies them.** Instead of one conflict at PR time, you now get a
  potential conflict at every group boundary — worktree vs. current HEAD,
  every time a parallel group finishes. That surfaces problems earlier
  (good) but adds friction on every run that has more than one group
  (real cost, not free).
- **The Tester's test command is fixed and unscoped.** `testing.command`
  runs the same way for every subtask, at the worktree root — there's no
  attempt to select or limit tests to just the subtask's files. For a
  large repo with a slow full suite, that's real wall-clock cost paid on
  every subtask and every revision cycle, not just once at PR time. A
  smarter setup (test impact analysis, per-subtask test filtering) is
  possible but is the caller's responsibility to encode into
  `testing.command` itself — the orchestrator doesn't attempt it.
  Independent verification (see `write_and_run_tests()`'s real subprocess
  run) is a hard requirement it enforces; being *fast* about it is not.
- **"Few agents always" is stated intent, not an enforced limit.** Stub +
  N implementers + tester + reviewer, repeated across iterations and
  groups, is a bigger fan-out than the original two-role design. Nothing
  in the plan yet caps concurrent agents at a number — "few" needs to
  become an actual constant before this is a control instead of a hope.
- **Iteration cap is a safety valve, not a fix** — hitting it means a
  deterministic, blocking human gate, not a heuristic "may" escalate that
  the orchestrator can quietly skip.
- **Babysitting has its own blast radius** — it pushes commits to a branch
  autonomously after the human thinks the PR is "just waiting for CI."
  Needs its own cap (iterations and wall clock), clear escalation instead
  of endless retries on a flaky check, and must never touch anything the
  human has since pushed to the branch by hand (detect via HEAD SHA
  check, back off, don't overwrite).
- **Base-branch drift is merged in, never rebased.** A rebase rewrites the
  feature branch's SHAs and requires a force-push to update the remote PR
  branch — which both defeats the HEAD-SHA-based manual-push detection
  above (the babysitter couldn't tell its own rebase apart from a human's
  push using that same check) and risks silently rewriting history under
  a human who has the PR branch checked out locally to review it. Merge
  achieves the same goal (branch caught up, conflicts surfaced early)
  without either problem, at the cost of a non-linear history.
- **Base-drift detection is local-only — it does not `git fetch`.** It
  compares the base branch's local ref, not `origin/<base_branch>`. If
  nothing else keeps the local base branch up to date with the remote
  (the human's own `git pull`, a cron, CI, etc.), drift landing only on
  the remote will not be seen. This is a real gap, not a design choice —
  worth closing if the orchestrator ever runs somewhere the local repo
  isn't already kept in sync some other way. Note this is only about
  babysitting a *single* task's PR (step [7]) — `campaign.py`'s
  between-task `sync_base_branch()` does fetch, since it has to pick up
  the just-merged previous task before branching for the next one.
- **The feature/task breakdown ([0a]/[0b] above) does not get the plan
  critic's independent second opinion.** Each *task's own* subtask plan
  still does (step [1b] runs per task, unchanged), but the higher-tier
  decomposition itself — how many features, how a task is scoped, where
  the dependency edges are — currently only gets the human's read at
  [0b]. Extending `PlanCriticClientBase` to this tier is a real option
  later; it just isn't built yet.
- **The merge-wait ([0e] above) is intentionally unbounded** — no
  iteration/wall-clock cap like babysitting has, because there's no
  failure mode to escalate on, only a human action (merging the PR) to
  wait for. This only works in practice because progress is persisted to
  the state file after every step: the wait is meant to be killed and
  resumed (laptop sleeps, terminal closes, days pass), not sat through in
  one uninterrupted process run.
- **A campaign halts, it doesn't self-heal, on anything unexpected** — an
  aborted/taken-over/escalated task, or a PR closed without merging, all
  stop the whole sequence rather than attempting a workaround. This is
  the same "hard stop, escalate to a human" philosophy as everywhere else
  in this system, just applied one tier up.
