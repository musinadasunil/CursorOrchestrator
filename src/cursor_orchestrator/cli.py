from __future__ import annotations

import argparse
import sys

from cursor_orchestrator.clients.cursor_cli_client import CursorCliClient
from cursor_orchestrator.clients.github_api_client import GithubApiClient, GithubApiError
from cursor_orchestrator.clients.mock_clients import (
    MockCursorClient,
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
    parser.add_argument("prompt", help="the natural-language prompt describing the work")
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
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(main())
