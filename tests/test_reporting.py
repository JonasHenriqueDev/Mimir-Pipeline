import json

from tcc_pipeline.models import Attempt, Issue, RunResult, Snapshot
from tcc_pipeline.reporting import compare_runs, generate_report, load_run_results, summarize_run


def make_run(condition="filtered", **changes):
    issue = Issue(
        key="one",
        rule="python:test",
        path="main.py",
        message="Issue",
        severity="MAJOR",
        effort_minutes=5,
    )
    values = dict(
        run_id=condition,
        experiment_id="exp",
        project="demo",
        condition=condition,
        base_commit="abc",
        status="completed",
        started_at="2026-01-01T10:00:00+00:00",
        ended_at="2026-01-01T10:00:10+00:00",
        baseline=Snapshot(issues=[issue]),
        final=Snapshot(issues=[]),
        simulated=True,
        llm_usage={"input_tokens": None, "output_tokens": None, "cost_usd": None},
    )
    values.update(changes)
    return RunResult(**values)


def write_run(directory, run):
    folder = directory / run.run_id
    folder.mkdir(parents=True)
    (folder / "result.json").write_text(run.model_dump_json(), encoding="utf-8")


def test_summary_preserves_multiplicity_and_unknown_tokens():
    run = make_run()
    run.baseline.issues.append(run.baseline.issues[0])
    run.final.issues.append(run.baseline.issues[0])
    row = summarize_run(run)
    assert row["resolved_issues"] == 1
    assert row["initial_issues"] == 2
    assert row["total_tokens"] is None
    assert row["initial_effort_minutes"] == 10
    assert row["duration_seconds"] == 10


def test_pairing_excludes_incomplete_or_mismatched_baseline():
    filtered, unfiltered = make_run(), make_run("unfiltered")
    assert len(compare_runs([filtered, unfiltered])["pairs"]) == 1
    unfiltered.status = "failed"
    assert compare_runs([filtered, unfiltered])["excluded"][0]["reason"] == "incomplete_arm"
    unfiltered.status = "completed"
    unfiltered.baseline.metrics["ncloc"] = 99
    assert compare_runs([filtered, unfiltered])["excluded"][0]["reason"] == "incomparable_baseline"


def test_pairing_does_not_mix_experiments_or_duplicate_arms():
    filtered, unfiltered = make_run(), make_run("unfiltered", experiment_id="other")
    assert compare_runs([filtered, unfiltered])["pairs"] == []
    extra = make_run(run_id="duplicate")
    assert compare_runs([filtered, make_run("unfiltered"), extra])["pairs"] == []


def test_report_escapes_source_text_and_generates_all_artifacts(tmp_path):
    run = make_run(project="<script>alert('x')</script>")
    run.attempts = [
        Attempt(
            index=1,
            iteration=1,
            issue=run.baseline.issues[0],
            status="infra_error",
            reason="failure",
        )
    ]
    write_run(tmp_path, run)
    report = generate_report(tmp_path)
    content = report.read_text(encoding="utf-8")
    assert "<script>" not in content
    assert "&lt;script&gt;" in content
    assert "DADOS SIMULADOS" in content
    assert "data:image/png;base64," in content
    for name in ["summary.csv", "attempts.csv", "comparison.json", "chart.png"]:
        assert (tmp_path / name).stat().st_size > 0
    assert json.loads((tmp_path / "comparison.json").read_text(encoding="utf-8"))["pairs"] == []
    row = summarize_run(run)
    assert row["infrastructure_errors"] == 1
    assert row["validation_failures"] == 0


def test_missing_snapshots_are_unknown_not_zero():
    row = summarize_run(make_run(baseline=None, final=None, status="failed"))
    assert row["initial_issues"] is None
    assert row["resolved_issues"] is None


def test_no_results_errors(tmp_path):
    import pytest

    with pytest.raises(ValueError, match="Nenhum"):
        load_run_results(tmp_path)


def test_repository_result_fixture_is_not_treated_as_run(tmp_path):
    write_run(tmp_path / "runs", make_run())
    fixture = tmp_path / "workspace" / "repository" / "result.json"
    fixture.parent.mkdir(parents=True)
    fixture.write_text('{"unrelated": true}', encoding="utf-8")
    assert len(load_run_results(tmp_path)) == 1


def test_pairing_detects_analyzer_version_and_rule_profile_drift():
    left, right = make_run(), make_run("unfiltered")
    left.baseline.metadata = {"server_version": "26.8"}
    right.baseline.metadata = {"server_version": "26.9"}
    assert compare_runs([left, right])["excluded"][0]["reason"] == "incomparable_baseline"
