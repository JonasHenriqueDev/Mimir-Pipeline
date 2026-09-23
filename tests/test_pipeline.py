import json
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mimir_pipeline.cli import app
from mimir_pipeline.demo import SOURCE, create_demo
from mimir_pipeline.models import Edit, PatchProposal
from mimir_pipeline.orchestration import execute
from mimir_pipeline.reporting import compare_runs, load_run_results
from mimir_pipeline.workspace import git


def test_end_to_end_pair_preserves_original_and_rejects_regression(tmp_path):
    config = create_demo(tmp_path)
    original_commit = git(Path(config.project.repo), "rev-parse", "HEAD")
    experiment_dir = execute(config, progress=lambda _: None)
    runs = {result.condition: result for result in load_run_results(experiment_dir)}
    assert all(run.status == "completed" for run in runs.values())
    assert all(run.base_commit == original_commit for run in runs.values())
    assert all(
        len(run.baseline.issues) == 2 and len(run.final.issues) == 1 for run in runs.values()
    )
    assert [attempt.status for attempt in runs["filtered"].attempts] == ["accepted", "filtered"]
    assert [attempt.status for attempt in runs["unfiltered"].attempts] == [
        "accepted",
        "validation_failed",
    ]
    assert len(compare_runs(list(runs.values()))["pairs"]) == 1
    assert (Path(config.project.repo) / "app.py").read_text(encoding="utf-8") == SOURCE
    assert git(Path(config.project.repo), "status", "--porcelain") == ""
    for run in runs.values():
        accepted = Path(run.worktree)
        assert "DEMO_UNUSED" not in (accepted / "app.py").read_text(encoding="utf-8")
        assert "DEMO_DYNAMIC" in (accepted / "app.py").read_text(encoding="utf-8")
        assert git(accepted, "status", "--porcelain") == ""
        attempt_dir = Path(run.attempts[0].artifact_dir)
        assert (attempt_dir / "patch.diff").is_file()
        assert (attempt_dir / "context.json").is_file()
        assert (attempt_dir / "sonar" / "snapshot.json").is_file()


def test_failed_baseline_never_calls_llm(tmp_path):
    config = create_demo(tmp_path)
    config.project.test_commands = [["{python}", "-c", "raise SystemExit(1)"]]
    result_dir = execute(config, ["filtered"], progress=lambda _: None)
    result = load_run_results(result_dir)[0]
    assert result.status == "failed"
    assert result.baseline is None
    assert result.attempts == []
    assert result.llm_usage["calls"] == 0
    assert "Referência funcional inválida" in result.error


def test_catalog_retains_multiple_experiments(tmp_path):
    config = create_demo(tmp_path)
    for _ in range(2):
        execute(config, ["filtered"], progress=lambda _: None)
    with sqlite3.connect(tmp_path / "catalog.sqlite3") as db:
        count = db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    assert count == 2


def test_missing_api_key_is_recorded_as_failed_run(tmp_path, monkeypatch):
    config = create_demo(tmp_path)
    config.sonar.backend = "sonar"
    config.llm.provider = "openai"
    config.llm.model = "example-model"
    monkeypatch.delenv("SONAR_TOKEN", raising=False)
    result_dir = execute(config, ["filtered"], progress=lambda _: None)
    result = load_run_results(result_dir)[0]
    assert result.status == "failed"
    assert "SONAR_TOKEN" in result.error
    assert result.attempts == []


def test_llm_error_is_distinguished_from_infrastructure(tmp_path, monkeypatch):
    from mimir_pipeline.llm import LLMError, MockLLM

    def refuse(self, issue, context):
        raise LLMError("Resposta recusada")

    monkeypatch.setattr(MockLLM, "propose", refuse)
    result_dir = execute(create_demo(tmp_path), ["unfiltered"], progress=lambda _: None)
    result = load_run_results(result_dir)[0]
    assert result.status == "failed"
    assert result.attempts[0].status == "llm_error"


def test_call_budget_stops_without_applying_an_unvalidated_patch(tmp_path):
    config = create_demo(tmp_path)
    config.llm.max_calls = 1
    result_dir = execute(config, ["filtered"], progress=lambda _: None)
    result = load_run_results(result_dir)[0]
    assert result.status == "completed"
    assert result.stop_reason == "llm_budget"
    assert len(result.final.issues) == 2
    assert all(attempt.status != "accepted" for attempt in result.attempts)


def test_new_issue_rejects_candidate_and_keeps_accepted_baseline(tmp_path, monkeypatch):
    from mimir_pipeline.llm import MockLLM

    def wrong_fix(self, issue, context):
        line = self._line(issue, context)
        return PatchProposal(
            edits=[
                Edit(
                    path=issue.path,
                    old_text=line,
                    new_text="    another_unused = 456  # DEMO_UNUSED\n",
                )
            ],
            explanation="test",
            expected_effect="test",
            risks=[],
        )

    monkeypatch.setattr(MockLLM, "propose", wrong_fix)
    result_dir = execute(create_demo(tmp_path), ["unfiltered"], progress=lambda _: None)
    result = load_run_results(result_dir)[0]
    assert result.status == "completed"
    assert result.attempts[0].status == "rejected"
    assert len(result.final.issues) == 2


def test_cli_demo_and_blind_label_export(tmp_path):
    cli = CliRunner()
    result = cli.invoke(app, ["demo", "--output", str(tmp_path)])
    assert result.exit_code == 0, result.output
    directory = next(tmp_path.glob("exp-*"))
    assert (directory / "report.html").is_file()
    exported = tmp_path / "labels.csv"
    result = cli.invoke(app, ["labels-export", str(directory), "--output", str(exported)])
    assert result.exit_code == 0, result.output
    content = exported.read_text(encoding="utf-8-sig")
    assert "manual_label" in content
    assert "prediction" not in content
    result = cli.invoke(app, ["labels-evaluate", str(directory), "--labels", str(exported)])
    assert result.exit_code == 0, result.output
    evaluation = json.loads(
        (directory / "evaluation" / "evaluation.json").read_text(encoding="utf-8")
    )
    assert evaluation["baseline_issues_without_label"] == 2
    assert evaluation["aggregate"]["false_positive_precision"] is None


def test_output_inside_source_is_rejected(tmp_path):
    config = create_demo(tmp_path)
    config.output_dir = str(Path(config.project.repo) / "results")
    with pytest.raises(ValueError, match="fora do repositório"):
        execute(config)


def test_ineligible_files_do_not_consume_llm_calls(tmp_path):
    config = create_demo(tmp_path)
    config.project.protected_globs.append("app.py")
    result_dir = execute(config, ["filtered"], progress=lambda _: None)
    result = load_run_results(result_dir)[0]
    assert result.status == "completed"
    assert result.stop_reason == "no_eligible_issues"
    assert result.llm_usage["calls"] == 0
    assert len(result.final.issues) == 2
