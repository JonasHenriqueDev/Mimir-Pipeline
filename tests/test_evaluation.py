import csv
import json

import pytest

from mimir_pipeline.evaluation import evaluate_labels, export_labels
from mimir_pipeline.models import Attempt, Classification, Issue, RunResult, Snapshot


def fixture_run(tmp_path):
    issues = [
        Issue(key=str(index), rule="python:test", path="main.py", message=f"Issue {index}")
        for index in range(3)
    ]
    attempts = [
        Attempt(
            index=index + 1,
            iteration=1,
            issue=issue,
            status="filtered" if index == 0 else "rejected",
            classification=Classification(label=label, rationale="MODEL SECRET", evidence=[]),
        )
        for index, (issue, label) in enumerate(
            zip(issues, ["false_positive", "inconclusive", "pertinent"])
        )
    ]
    attempts.append(
        Attempt(
            index=4,
            iteration=2,
            issue=issues[0],
            status="accepted",
            classification=Classification(label="pertinent", rationale="Repeated", evidence=[]),
        )
    )
    run = RunResult(
        run_id="one",
        experiment_id="exp",
        project="demo",
        condition="filtered",
        base_commit="abc",
        status="completed",
        started_at="2026-01-01T00:00:00+00:00",
        baseline=Snapshot(issues=issues),
        final=Snapshot(issues=[]),
        attempts=attempts,
        simulated=True,
    )
    (tmp_path / "result.json").write_text(run.model_dump_json(), encoding="utf-8")
    return run


def fill_labels(path, labels):
    with path.open(encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        fields, rows = reader.fieldnames, list(reader)
    for row, label in zip(rows, labels):
        row["manual_label"] = label
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return rows, fields


def test_export_is_blind_and_preserves_existing_annotations(tmp_path):
    fixture_run(tmp_path)
    path = export_labels(tmp_path, tmp_path / "labels.csv")
    text = path.read_text(encoding="utf-8-sig")
    assert "MODEL SECRET" not in text
    assert "false_positive" not in text
    assert "condition" not in text
    assert len(list(csv.DictReader(text.splitlines()))) == 3
    with pytest.raises(FileExistsError):
        export_labels(tmp_path, path)


def test_first_prediction_only_and_unknown_coverage(tmp_path):
    fixture_run(tmp_path)
    path = export_labels(tmp_path, tmp_path / "labels.csv")
    fill_labels(path, ["pertinent", "false_positive", ""])
    report_path = evaluate_labels(tmp_path, path, tmp_path / "evaluation")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    metrics = report["aggregate"]
    assert metrics["observations"] == 3
    assert metrics["true_problems_classified_for_drop"] == 1
    assert metrics["true_problems_actually_filtered"] == 1
    assert metrics["model_inconclusive"] == 1
    assert metrics["manual_label_unknown"] == 1
    assert metrics["false_positive_recall"] == 0
    assert metrics["false_positive_precision"] == 0
    assert report["baseline_issues_without_label"] == 1


def test_duplicates_and_conflicts_are_rejected(tmp_path):
    fixture_run(tmp_path)
    path = export_labels(tmp_path, tmp_path / "labels.csv")
    rows, fields = fill_labels(path, ["pertinent", "false_positive", "inconclusive"])
    with path.open("a", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writerow({**rows[0], "manual_label": "false_positive"})
    with pytest.raises(ValueError, match="duplicada ou conflitante"):
        evaluate_labels(tmp_path, path, tmp_path / "evaluation")


def test_no_reference_labels_does_not_invent_accuracy(tmp_path):
    fixture_run(tmp_path)
    path = export_labels(tmp_path, tmp_path / "labels.csv")
    output = evaluate_labels(tmp_path, path, tmp_path / "evaluation")
    metrics = json.loads(output.read_text(encoding="utf-8"))["aggregate"]
    assert metrics["false_positive_precision"] is None
    assert metrics["false_positive_recall"] is None
    assert metrics["accuracy_on_decisive_predictions_and_determinate_labels"] is None
    assert metrics["manual_label_unknown"] == 3


def test_unknown_identity_and_bad_label_are_rejected(tmp_path):
    fixture_run(tmp_path)
    path = tmp_path / "labels.csv"
    path.write_text("issue_identity,manual_label\nunknown,pertinent\n", encoding="utf-8")
    with pytest.raises(ValueError, match="desconhecida"):
        evaluate_labels(tmp_path, path, tmp_path / "evaluation")
