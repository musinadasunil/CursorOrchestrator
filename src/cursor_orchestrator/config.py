from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "config.yaml"


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ModelsConfig:
    planner: str
    implementer: str
    tester: str
    reviewer: str


@dataclass(frozen=True)
class LimitsConfig:
    max_parallel_agents: int
    max_review_iterations: int
    max_babysit_iterations: int
    max_babysit_wall_clock_minutes: int
    babysit_poll_interval_seconds: float


@dataclass(frozen=True)
class GitConfig:
    base_branch: str
    pr_backend: str  # "gh" | "api" -- see config.yaml's comment


@dataclass(frozen=True)
class OrchestratorConfig:
    models: ModelsConfig
    limits: LimitsConfig
    git: GitConfig

    @staticmethod
    def from_yaml(path: Path | str = DEFAULT_CONFIG_PATH) -> "OrchestratorConfig":
        path = Path(path)
        if not path.exists():
            raise ConfigError(f"config file not found: {path}")

        with path.open() as f:
            raw = yaml.safe_load(f) or {}

        config = OrchestratorConfig(
            models=ModelsConfig(**_section(raw, "models")),
            limits=LimitsConfig(**_section(raw, "limits")),
            git=GitConfig(**_section(raw, "git")),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if self.models.reviewer == self.models.implementer:
            raise ConfigError(
                "models.reviewer must differ from models.implementer -- "
                "the whole point of review is a second opinion from a "
                "different model. Point them at different models in "
                "config.yaml."
            )
        if self.limits.max_parallel_agents < 1:
            raise ConfigError("limits.max_parallel_agents must be >= 1")
        if self.limits.max_review_iterations < 1:
            raise ConfigError("limits.max_review_iterations must be >= 1")
        if self.limits.max_babysit_iterations < 1:
            raise ConfigError("limits.max_babysit_iterations must be >= 1")
        if self.limits.max_babysit_wall_clock_minutes < 1:
            raise ConfigError("limits.max_babysit_wall_clock_minutes must be >= 1")
        if self.limits.babysit_poll_interval_seconds <= 0:
            raise ConfigError("limits.babysit_poll_interval_seconds must be > 0")
        if self.git.pr_backend not in ("gh", "api"):
            raise ConfigError(
                f"git.pr_backend must be 'gh' or 'api', got {self.git.pr_backend!r}"
            )


def _section(raw: dict, name: str) -> dict:
    section = raw.get(name)
    if not isinstance(section, dict):
        raise ConfigError(f"config file missing required section: {name}")
    return section
