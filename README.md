# cursor-orchestrator

Prompt -> plan -> human scope gate -> build -> review -> PR -> babysit.
Never auto-merges -- a human always approves and merges. See `PLAN.md` for
the full architecture and design rationale.

## Install (global command)

```
brew install pipx   # if you don't already have it
pipx ensurepath
pipx install .      # from this repo root
```

For active development on the tool itself, use an editable install so
code edits take effect without reinstalling:

```
pipx install -e .
```

This puts `cursor-orchestrator` on your `PATH` as a global command.

## Usage

```
cursor-orchestrator --dry-run "add input validation to the signup form"
cursor-orchestrator "add input validation to the signup form"
cursor-orchestrator --prompt-file ./prompt.md
```

`--dry-run` uses mock clients (no network calls, no real PR) and walks
the entire state machine -- plan, human scope-approval prompt, a
simulated revision cycle, and a simulated CI-failure-then-fix during
babysitting -- so you can see the whole flow before pointing it at a
real repo.

The prompt can come from a `.md`/`.txt` file instead of the command
line via `--prompt-file`/`-f` (its contents are read and stripped) --
useful once the prompt is long, e.g. a whole architecture description
for `--sequential` below. Pass exactly one of the positional prompt or
`--prompt-file`, not both.

### `--sequential`: an entire architecture, not just one prompt

```
cursor-orchestrator --dry-run --sequential --prompt-file ./architecture.md
cursor-orchestrator --sequential --prompt-file ./architecture.md
```

Instead of one prompt -> one PR, `--sequential` treats the prompt as a
whole architecture: it's decomposed into features, each into small
tasks sized so a single resulting PR stays reviewable by one person.
Tasks are built **one at a time** -- each goes through the exact same
plan -> human approval -> build -> review -> PR flow as a normal run,
but this time the tool actually **waits for a human to merge that PR**
(not just for CI to go green, which is as far as a normal run waits)
before syncing `main` and branching off for the next task.

That wait is unbounded and can take a long time -- it's meant to be
interrupted (Ctrl-C, a closed laptop, a dropped SSH session) and
resumed, not sat through in one process. Progress is persisted to a
state file after every step (default under
`~/.cursor-orchestrator/campaigns/`, overridable with `--state-file`);
re-running the identical command detects it and resumes -- skipping
feature-planning and any already-merged task -- instead of starting the
whole architecture over.

If a task's build gets aborted, taken over, or escalated, or its PR gets
closed without merging, the whole campaign halts there and prints which
task needs attention -- it does not attempt to skip ahead or guess a fix.

Known simplification: the plan critic's independent second opinion
still applies to each task's own subtask plan, but not yet to the
higher-level feature/task breakdown itself -- that only gets the
human's read at the one approval gate.

A real (non-dry-run) run needs:
- the `cursor-agent` CLI installed and authenticated (verify its flags
  against your installed version -- see the note in
  `src/cursor_orchestrator/clients/cursor_cli_client.py`)
- a way to talk to GitHub for PR/CI operations -- either the `gh` CLI
  (`gh auth login`), or, if `gh` isn't installable/allowed on this
  machine, GitHub's REST API directly over HTTPS. Set `git.pr_backend` in
  `config.yaml` to `"gh"` (default) or `"api"`. The `"api"` backend needs
  no CLI at all, just a `GITHUB_TOKEN` (or `GH_TOKEN`) environment
  variable with repo/PR scope -- owner/repo and the API host are
  auto-detected from the repo's `origin` remote (GitHub.com and GitHub
  Enterprise both work).
- `testing.command` in `config.yaml` set to whatever actually runs this
  repo's tests (e.g. `"pytest -q"`, `"npm test"`, `"go test ./..."`) --
  this is run independently by the orchestrator itself in each subtask's
  worktree, and its real exit code is what `TestResult.passed` comes from.
  The Tester agent only writes the test code; it is never trusted to
  self-report whether its own tests pass.

Planner, plan critic, implementer, tester, and reviewer are all
`cursor-agent`, just pointed at different `--model` values per
`config.yaml`. Two of those five are enforced (at config load time) to
differ from the role they're independently checking, so "independent
second opinion" is a structural guarantee, not a convention:
- `models.reviewer` must differ from `models.implementer`
- `models.plan_critic` must differ from `models.planner`

The plan critic runs right after planning and before the human
scope-approval prompt -- it critiques the subtask decomposition itself
(sizing, missing/unnecessary dependencies, prompt coverage, scope creep)
and its findings are shown alongside the plan. It's advisory only: it has
no verdict and can't block anything on its own, the same way the code
reviewer's findings are a second opinion for the human merging the PR,
not an automatic gate.

## Config

Every tunable and model name lives in `config.yaml`
(`src/cursor_orchestrator/config.yaml` in this repo) -- it ships bundled
with the package, so it's found the same way whether you installed
editable (`pipx install -e .`) or not. `config.py` validates it at load
time -- in particular, `models.reviewer` must differ from
`models.implementer`, and `models.plan_critic` must differ from
`models.planner`, since that's the whole point of an independent review.

Every `cursor-agent`/`gh` subprocess call also has a hard timeout
(`limits.cursor_agent_timeout_seconds`, `limits.gh_timeout_seconds`) --
without one, a single hung process would hang the whole orchestrator
forever. Raise these if you hit false-positive timeouts on legitimately
slow tasks.

`limits.merge_poll_interval_seconds` only matters for `--sequential`: how
often it checks whether a task's PR has been merged yet. Unlike the caps
above, there's no matching wall-clock/iteration cap on this wait --
waiting for a human to merge is the point, not a failure mode.

To use your own settings without editing the installed copy, copy that
file somewhere and pass `--config /path/to/your-config.yaml`.

## Tests

```
pip install -e ".[dev]"
pytest tests/
```
