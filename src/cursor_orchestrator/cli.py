from __future__ import annotations

import argparse
import sys
from pathlib import Path

from cursor_orchestrator.campaign import CampaignRunner, default_state_file_path
from cursor_orchestrator.clients.cursor_cli_client import CursorCliClient
from cursor_orchestrator.clients.github_api_client import GithubApiClient, GithubApiError
from cursor_orchestrator.clients.mock_clients import (
    MockCursorClient,
    MockFeaturePlannerClient,
    MockPlanCriticClient,
    MockReviewerClient,
    MockTestClient,
)
from cursor_orchestrator.config import DEFAULT_CONFIG_PATH, ConfigError, OrchestratorConfig
from cursor_orchestrator.orchestrator import Orchestrator


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    try:
        config = OrchestratorConfig.from_yaml(args.config)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 1

    if args.dry_run:
        cursor_client = MockCursorClient()
        planner_client = cursor_client
        plan_critic_client = MockPlanCriticClient()
        test_client = MockTestClient()
        reviewer_client = MockReviewerClient()
        feature_planner_client = MockFeaturePlannerClient()
    else:
        def _agent(model: str) -> CursorCliClient:
            return CursorCliClient(
                model=model,
                cursor_agent_timeout_seconds=config.limits.cursor_agent_timeout_seconds,
                gh_timeout_seconds=config.limits.gh_timeout_seconds,
            )

        planner_client = _agent(config.models.planner)
        plan_critic_client = _agent(config.models.plan_critic)
        implementer_agent = _agent(config.models.implementer)
        if config.git.pr_backend == "api":
            try:
                cursor_client = GithubApiClient(agent_client=implementer_agent, repo_path=args.repo)
            except GithubApiError as e:
                print(f"github api client error: {e}", file=sys.stderr)
                return 1
        else:
            cursor_client = implementer_agent
        test_client = CursorCliClient(
            model=config.models.tester,
            cursor_agent_timeout_seconds=config.limits.cursor_agent_timeout_seconds,
            gh_timeout_seconds=config.limits.gh_timeout_seconds,
            test_command=config.testing.command,
            test_timeout_seconds=config.testing.timeout_seconds,
        )
        reviewer_client = _agent(config.models.reviewer)
        # Same role/model as the planner -- feature planning is just a
        # coarser-grained pass of the same job, not a separate model to
        # configure.
        feature_planner_client = planner_client

    if args.sequential:
        state_file = (
            Path(args.state_file)
            if args.state_file
            else default_state_file_path(args.repo, args.prompt)
        )
        campaign_result = CampaignRunner(
            config=config,
            cursor_client=cursor_client,
            planner_client=planner_client,
            plan_critic_client=plan_critic_client,
            test_client=test_client,
            reviewer_client=reviewer_client,
            feature_planner_client=feature_planner_client,
            dry_run=args.dry_run,
            repo_path=args.repo,
        ).run(args.prompt, state_file)

        if campaign_result.aborted:
            print("Aborted by human at the feature-plan approval gate.")
            return 1
        if campaign_result.halted_reason:
            print(f"Campaign halted: {campaign_result.halted_reason}")
            return 1
        print("Campaign complete: every task merged.")
        return 0

    orchestrator = Orchestrator(
        config=config,
        cursor_client=cursor_client,
        planner_client=planner_client,
        plan_critic_client=plan_critic_client,
        test_client=test_client,
        reviewer_client=reviewer_client,
        dry_run=args.dry_run,
        repo_path=args.repo,
    )

    result = orchestrator.run(args.prompt)

    if result.aborted:
        print("Aborted by human at scope approval.")
        return 1
    if result.took_over:
        print("Human took over manually at the iteration-cap gate.")
        return 1

    print(f"PR opened: {result.pr_url}")
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="cursor-orchestrator",
        description="prompt -> plan -> build -> review -> PR -> babysit. Never auto-merges.",
    )
    prompt_source = parser.add_mutually_exclusive_group(required=True)
    prompt_source.add_argument(
        "prompt", nargs="?", default=None, help="the natural-language prompt describing the work"
    )
    prompt_source.add_argument(
        "--prompt-file",
        "-f",
        help="path to a .md/.txt file whose contents are used as the prompt, instead of "
        "passing the prompt inline",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="walk the full flow with mock clients -- no network calls, no real PR",
    )
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG_PATH,
        help="path to config.yaml (default: the copy bundled with the installed package)",
    )
    parser.add_argument(
        "--repo", default=".", help="path to the git repo to operate in (ignored in --dry-run)"
    )
    parser.add_argument(
        "--sequential",
        action="store_true",
        help="treat the prompt as an entire architecture: decompose into features and small, "
        "PR-sized tasks, then build them one at a time, each in its own branch/PR, waiting for "
        "a human to actually merge each PR before syncing main and starting the next task",
    )
    parser.add_argument(
        "--state-file",
        default=None,
        help="path to the --sequential campaign's progress file (default: derived from --repo "
        "and the prompt under ~/.cursor-orchestrator/campaigns/). Re-running with the same "
        "state file resumes an interrupted campaign instead of starting over",
    )
    args = parser.parse_args(argv)

    if args.prompt_file:
        path = Path(args.prompt_file)
        if not path.is_file():
            parser.error(f"--prompt-file not found: {path}")
        contents = path.read_text().strip()
        if not contents:
            parser.error(f"--prompt-file is empty: {path}")
        args.prompt = contents

    return args


if __name__ == "__main__":
    sys.exit(main())
