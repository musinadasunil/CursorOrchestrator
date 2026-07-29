import pytest

from cursor_orchestrator.models import Feature, FeaturePlan, Plan, SubTask, Task


def _plan(*subtasks: SubTask) -> Plan:
    return Plan(summary="test plan", subtasks=list(subtasks))


def test_parallel_groups_batches_independent_subtasks_together():
    plan = _plan(
        SubTask(id="a", description="a"),
        SubTask(id="b", description="b"),
        SubTask(id="c", description="c", depends_on=["a", "b"]),
    )
    groups = plan.parallel_groups()
    assert {t.id for t in groups[0]} == {"a", "b"}
    assert [t.id for t in groups[1]] == ["c"]


def test_parallel_groups_linear_chain_is_one_per_group():
    plan = _plan(
        SubTask(id="a", description="a"),
        SubTask(id="b", description="b", depends_on=["a"]),
        SubTask(id="c", description="c", depends_on=["b"]),
    )
    groups = plan.parallel_groups()
    assert [[t.id for t in g] for g in groups] == [["a"], ["b"], ["c"]]


def test_parallel_groups_raises_on_unknown_dependency():
    plan = _plan(SubTask(id="a", description="a", depends_on=["missing"]))
    with pytest.raises(ValueError, match="unknown subtask"):
        plan.parallel_groups()


def test_parallel_groups_raises_on_cycle():
    plan = _plan(
        SubTask(id="a", description="a", depends_on=["b"]),
        SubTask(id="b", description="b", depends_on=["a"]),
    )
    with pytest.raises(ValueError, match="cycle"):
        plan.parallel_groups()


def test_parallel_groups_raises_on_duplicate_subtask_ids():
    # Planner output is model-generated, not guaranteed unique -- a
    # collision must fail loudly rather than silently dropping one of the
    # colliding subtasks from execution.
    plan = _plan(
        SubTask(id="a", description="first a"),
        SubTask(id="a", description="second a, same id"),
    )
    with pytest.raises(ValueError, match="duplicate subtask ids"):
        plan.parallel_groups()


def test_chunked_parallel_groups_respects_cap():
    plan = _plan(*(SubTask(id=str(i), description=str(i)) for i in range(5)))
    chunks = plan.chunked_parallel_groups(max_parallel_agents=2)
    assert [len(c) for c in chunks] == [2, 2, 1]
    assert {t.id for chunk in chunks for t in chunk} == {"0", "1", "2", "3", "4"}


def _feature_plan(*features: Feature) -> FeaturePlan:
    return FeaturePlan(summary="test feature plan", features=list(features))


def test_ordered_tasks_respects_cross_feature_dependency():
    plan = _feature_plan(
        Feature(id="f1", description="f1", tasks=[Task(id="a", description="a")]),
        Feature(
            id="f2",
            description="f2",
            tasks=[Task(id="b", description="b", depends_on=["a"])],
        ),
    )
    assert [t.id for t in plan.ordered_tasks()] == ["a", "b"]


def test_ordered_tasks_preserves_declared_order_when_independent():
    plan = _feature_plan(
        Feature(
            id="f1",
            description="f1",
            tasks=[Task(id="a", description="a"), Task(id="b", description="b")],
        )
    )
    assert [t.id for t in plan.ordered_tasks()] == ["a", "b"]


def test_ordered_tasks_raises_on_duplicate_task_ids_across_features():
    plan = _feature_plan(
        Feature(id="f1", description="f1", tasks=[Task(id="a", description="first a")]),
        Feature(id="f2", description="f2", tasks=[Task(id="a", description="second a")]),
    )
    with pytest.raises(ValueError, match="duplicate task ids"):
        plan.ordered_tasks()


def test_ordered_tasks_raises_on_unknown_dependency():
    plan = _feature_plan(
        Feature(
            id="f1",
            description="f1",
            tasks=[Task(id="a", description="a", depends_on=["missing"])],
        )
    )
    with pytest.raises(ValueError, match="unknown task"):
        plan.ordered_tasks()


def test_ordered_tasks_raises_on_cycle():
    plan = _feature_plan(
        Feature(
            id="f1",
            description="f1",
            tasks=[
                Task(id="a", description="a", depends_on=["b"]),
                Task(id="b", description="b", depends_on=["a"]),
            ],
        )
    )
    with pytest.raises(ValueError, match="cycle"):
        plan.ordered_tasks()
