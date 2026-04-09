from tests.support.pytest_concurrency import (
    RecordedInterval,
    analyze_intervals,
    format_summary,
)


def test_analyze_intervals_reports_tail_bottleneck() -> None:
    summary = analyze_intervals(
        [
            RecordedInterval("tests/unit/test_a.py::test_long", "gw0", 0, 10_000_000_000),
            RecordedInterval("tests/unit/test_b.py::test_short", "gw1", 0, 2_000_000_000),
        ],
        requested_slots=2,
    )

    assert summary.requested_slots == 2
    assert summary.observed_workers == ("gw0", "gw1")
    assert summary.test_count == 2
    assert summary.wall_time_s == 10.0
    assert summary.estimated_savings_upper_bound_s == 4.0
    assert summary.estimated_balanced_runtime_floor_s == 6.0
    assert summary.avg_active == 1.2
    assert summary.max_active == 2
    assert summary.full_capacity_time_s == 2.0
    assert summary.concurrency_debt_s == 8.0
    assert len(summary.bottlenecks) == 1
    assert summary.bottlenecks[0].nodeid == "tests/unit/test_a.py::test_long"
    assert summary.bottlenecks[0].worker_id == "gw0"
    assert summary.bottlenecks[0].wall_time_s == 10.0
    assert summary.bottlenecks[0].concurrency_debt_s == 8.0


def test_analyze_intervals_splits_shared_concurrency_debt() -> None:
    summary = analyze_intervals(
        [
            RecordedInterval("tests/unit/test_a.py::test_first", "gw0", 0, 10_000_000_000),
            RecordedInterval("tests/unit/test_b.py::test_second", "gw1", 0, 10_000_000_000),
            RecordedInterval("tests/unit/test_c.py::test_short", "gw2", 0, 2_000_000_000),
        ],
        requested_slots=3,
    )

    assert summary.avg_active == 2.2
    assert summary.full_capacity_time_s == 2.0
    assert summary.concurrency_debt_s == 8.0
    assert summary.bottlenecks[0].nodeid == "tests/unit/test_a.py::test_first"
    assert summary.bottlenecks[0].concurrency_debt_s == 4.0
    assert summary.bottlenecks[1].nodeid == "tests/unit/test_b.py::test_second"
    assert summary.bottlenecks[1].concurrency_debt_s == 4.0

