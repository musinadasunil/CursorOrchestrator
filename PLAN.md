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
  each subtask: id, description, kind (stub | implement),
                files_likely_touched, depends_on[]
  builds the dependency graph → decides which subtasks form a
  sequential chain vs. an independent parallel group
  │
  ▼
[2] HUMAN SCOPE APPROVAL  ← blocking gate (approve / edit / abort)
  - "edit" loops back into the planner with the human's corrections,
    re-presented until explicitly approved — this is not a one-shot ask
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
Dataclasses: `TaskStatus` (enum), `SubTask` (now with a `kind: STUB |
IMPLEMENT` field), `Plan` (with `parallel_groups()` — topological
batching), `TestResult`, `ReviewFinding`, `ReviewResult`.

### `prompts.py`
- **Planner system prompt** — instructs decomposition into subtasks with
  explicit `depends_on` edges, marks which (if any) subtask is a
  stub/interface task that others depend on, and flags which subtasks (if
  any) warrant a different model due to task type (e.g., a
  security-sensitive subtask routed to a different model than routine
  CRUD).
- **Tester system prompt** — instructed to write tests for edge cases and
  failure paths, not just the happy path; instructed explicitly that a
  test run against a stub only proves contract shape, not behavior, and
  must be re-run against the real implementation.
- **Reviewer system prompt** — the checklist above, instructed to review
  the diff *blind first* (before seeing implementer rationale) to avoid
  anchoring, then reconcile. Forced to output severity-labeled findings,
  never a bare "approved."

### `clients/base.py`
Abstract interfaces: `CursorClientBase.plan()`, `.implement_subtask()`,
`.create_pr()`, `.get_pr_status()`, `.get_pr_review_comments()`,
`.push_fix_commit()`; `TestClientBase.write_and_run_tests()`;
`ReviewerClientBase.review()`. Everything else in the system talks to
these interfaces, not to a specific SDK — swap implementations without
touching orchestrator logic.

### `branch_manager.py`
Owns the single feature branch's lifecycle: creates it, opens/tears down
a worktree per subtask in the active group, merges each worktree back
into the feature branch sequentially once its group completes, and hard
stops (no auto-resolve) on merge conflict. This is the piece that makes
"one branch, dependency-ordered groups" real instead of aspirational —
without it, "parallel worktrees merging into one branch" is just prose.

### `clients/cursor_cli_client.py`
Real implementation via subprocess calls to the `cursor-agent` CLI —
plan, implement, test, *and* review are all `cursor-agent` invocations,
each constructed with that role's configured `--model`. The reviewer is
just another `cursor-agent` call pointed at a different model than the
implementer (`config.py` enforces `models.reviewer != models.implementer`
at load time, so "independent second opinion" is structural, not
convention). PR lifecycle and CI status go through the `gh` CLI instead,
since that's a hosting-provider concern, not an agent concern.
**Verify against your installed `cursor-agent --help` / current Cursor
docs before relying on this** — exact flags and the API surface have been
changing release to release; treat the flags in this file as a starting
point, not gospel.

### `clients/mock_clients.py`
Fully working fakes with no network calls — used to unit-test the
orchestration/state-machine logic (dependency batching, approval gate,
revision loop, iteration cap, PR payload construction) independent of any
live API.

### `orchestrator.py`
The state machine described in Flow above. Owns the group-by-group
execution, the deterministic iteration cap and human-gate hand-off at
[5b], and the human-approval blocking point at [2]. This is the part
that's actually unit-testable without your credentials, and the part I
ran `pytest` against before handing this over.

### `babysitter.py`
Owns step [7]. A polling loop (own iteration *and* wall-clock cap,
separate from the implementer↔tester↔reviewer cap in step [5]) that
watches CI status and human review comments on the opened PR, maps a CI
failure or a change-request comment onto the owning `SubTask`, and
re-enters the implementer for a targeted fix — never a full re-plan,
never a merge. Escalates to the human (PR comment + CLI notification) on
ambiguous input, a repeated failure of the same check, or cap
exhaustion, rather than retrying blindly.

### `pr_template.md`
Filled at step [6]: prompt, plan, per-subtask rationale, reviewer findings
(explicitly labeled as a second opinion to verify, not fact), test results.

### `cli.py`
Entry point: `python cli.py "build a feature that..."` — wires real clients
together, or `--dry-run` to use the mocks and walk the flow without
touching Cursor at all.

---

## Honest risk notes

- **Cursor CLI/API surface is a moving target** — the integration points in
  `cursor_cli_client.py` need a check against current docs before first
  real run; don't trust the flags as-written.
- **Reviewer findings are a second opinion, not ground truth** — the PR
  template labels them as such on purpose. Don't let that framing erode
  over time.
- **Single feature branch doesn't eliminate merge conflicts, it just moves
  and multiplies them.** Instead of one conflict at PR time, you now get a
  potential conflict at every group boundary — worktree vs. current HEAD,
  every time a parallel group finishes. That surfaces problems earlier
  (good) but adds friction on every run that has more than one group
  (real cost, not free).
- **Stub-first decomposition is a harder planning problem than file-level
  dependency ordering.** The planner now has to correctly identify
  interface boundaries, not just "what touches what." Get the stub's
  contract wrong and every implementer built against it is wrong — and
  you won't find out until the tester or reviewer catches it, later and
  less obviously than a merge conflict would.
- **Testing against a stub is a false-confidence trap if the plan doesn't
  enforce the re-run.** A green test run against a stub proves the stub's
  fake behavior, not the real implementation's. This only works if
  `orchestrator.py` mechanically forces a second test pass once the real
  implementation replaces the stub — if that's left to agent discipline
  instead of the state machine, it will get skipped under iteration
  pressure.
- **Three-way loop (Implementer / Tester / Reviewer) has no reconciliation
  rule yet.** If the tester says tests fail and the reviewer says
  `ready_for_pr` in the same iteration — or vice versa — the plan doesn't
  say who wins. Without an explicit precedence rule this can oscillate
  quietly across iterations and burn the cap without the human ever
  learning why.
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
