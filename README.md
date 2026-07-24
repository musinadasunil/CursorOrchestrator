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
```

`--dry-run` uses mock clients (no network calls, no real PR) and walks
the entire state machine -- plan, human scope-approval prompt, a
simulated revision cycle, and a simulated CI-failure-then-fix during
babysitting -- so you can see the whole flow before pointing it at a
real repo.

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

To use your own settings without editing the installed copy, copy that
file somewhere and pass `--config /path/to/your-config.yaml`.

## Tests

```
pip install -e ".[dev]"
pytest tests/
```
