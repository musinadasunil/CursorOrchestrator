import pytest

from cursor_orchestrator.models import Plan, SubTask


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


def test_chunked_parallel_groups_respects_cap():
    plan = _plan(*(SubTask(id=str(i), description=str(i)) for i in range(5)))
    chunks = plan.chunked_parallel_groups(max_parallel_agents=2)
    assert [len(c) for c in chunks] == [2, 2, 1]
    assert {t.id for chunk in chunks for t in chunk} == {"0", "1", "2", "3", "4"}
