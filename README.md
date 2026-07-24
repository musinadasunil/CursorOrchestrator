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
- the GitHub `gh` CLI installed and authenticated (`gh auth login`)

Planner, implementer, tester, and reviewer are all `cursor-agent`, just
pointed at different `--model` values per `config.yaml` -- the reviewer
is enforced (at config load time) to use a different model than the
implementer, so the "independent second opinion" is a structural
guarantee, not a convention.

## Config

Every tunable and model name lives in `config.yaml` (see that file, or
pass `--config /path/to/other.yaml`). `config.py` validates it at load
time -- in particular, `models.reviewer` must differ from
`models.implementer`, since that's the whole point of an independent
review.

## Tests

```
pip install -e ".[dev]"
pytest tests/
```
