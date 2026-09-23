"""Blinded manual labels and classification evaluation without invented ground truth."""

import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any

from .reporting import load_run_results, write_csv

LABELS = ("pertinent", "false_positive", "inconclusive")
EXPORT_FIELDS = [
    "issue_identity",
    "rule",
    "path",
    "line",
    "anchor",
    "message",
    "manual_label",
    "notes",
]


def export_labels(experiment_dir: Path, output_csv: Path) -> Path:
    """Export baseline issues only, with no predictions, condition or rationale."""
    issues = {}
    for run in load_run_results(Path(experiment_dir)):
        if run.baseline:
            for issue in run.baseline.issues:
                issues.setdefault(issue.identity, issue)
    rows = [
        {
            "issue_identity": identity,
            "rule": issue.rule,
            "path": issue.path,
            "line": issue.line,
            "anchor": issue.anchor,
            "message": issue.message,
            "manual_label": "",
            "notes": "",
        }
        for identity, issue in sorted(issues.items())
    ]
    output = Path(output_csv).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise FileExistsError(
            f"Arquivo de rótulos já existe; escolha outro caminho para preservar a anotação: {output}"
        )
    write_csv(output, rows, EXPORT_FIELDS)
    return output


def _read_labels(path: Path, known: set[str]) -> dict[str, str]:
    labels = {}
    with path.open(encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        if not {"issue_identity", "manual_label"}.issubset(reader.fieldnames or []):
            raise ValueError("CSV deve conter issue_identity e manual_label.")
        for line, row in enumerate(reader, 2):
            identity = row["issue_identity"] or ""
            # Undo only the spreadsheet protection added by our CSV exporter.
            if identity.startswith("'") and identity[1:] in known:
                identity = identity[1:]
            label = (row["manual_label"] or "").strip()
            if identity in labels:
                raise ValueError(f"Identidade duplicada ou conflitante na linha {line}: {identity}")
            if identity not in known:
                raise ValueError(f"Identidade desconhecida na linha {line}: {identity}")
            if label and label not in LABELS:
                raise ValueError(
                    f"Rótulo inválido na linha {line}: {label}. Use {', '.join(LABELS)}."
                )
            labels[identity] = label
    return labels


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _metrics(observations: list[dict[str, Any]]) -> dict[str, Any]:
    matrix = {actual: {predicted: 0 for predicted in LABELS} for actual in LABELS}
    manual_unknown = 0
    for observation in observations:
        actual, predicted = observation["manual_label"], observation["prediction"]
        if actual not in LABELS:
            manual_unknown += 1
        else:
            matrix[actual][predicted] += 1
    true_dropped = matrix["pertinent"]["false_positive"]
    false_identified = matrix["false_positive"]["false_positive"]
    false_total = sum(matrix["false_positive"].values())
    pertinent_total = sum(matrix["pertinent"].values())
    determinate_labels = false_total + pertinent_total
    model_inconclusive = sum(
        observation["prediction"] == "inconclusive" for observation in observations
    )
    model_decisive_labeled = (
        determinate_labels
        - matrix["pertinent"]["inconclusive"]
        - matrix["false_positive"]["inconclusive"]
    )
    return {
        "observations": len(observations),
        "confusion_matrix": matrix,
        "true_problems_classified_for_drop": true_dropped,
        "true_problems_actually_filtered": sum(
            observation["manual_label"] == "pertinent"
            and observation["prediction"] == "false_positive"
            and observation["filtered"]
            for observation in observations
        ),
        "false_positives_identified": false_identified,
        "false_positive_precision": _ratio(false_identified, false_identified + true_dropped),
        "false_positive_recall": _ratio(false_identified, false_total),
        "true_problem_drop_rate": _ratio(true_dropped, pertinent_total),
        "manual_label_unknown": manual_unknown,
        "manual_label_inconclusive": sum(matrix["inconclusive"].values()),
        "model_inconclusive": model_inconclusive,
        "manual_determinate_coverage": _ratio(determinate_labels, len(observations)),
        "model_decisive_coverage": _ratio(
            len(observations) - model_inconclusive, len(observations)
        ),
        "model_decisive_coverage_on_determinate_labels": _ratio(
            model_decisive_labeled, determinate_labels
        ),
        "accuracy_on_decisive_predictions_and_determinate_labels": _ratio(
            matrix["pertinent"]["pertinent"] + false_identified, model_decisive_labeled
        ),
    }


def evaluate_labels(experiment_dir: Path, labels_csv: Path, output_dir: Path) -> Path:
    runs = load_run_results(Path(experiment_dir))
    known = {issue.identity for run in runs if run.baseline for issue in run.baseline.issues}
    labels = _read_labels(Path(labels_csv), known)
    all_observations, per_run = [], []
    for run in runs:
        predictions = {}
        for attempt in sorted(run.attempts, key=lambda item: item.index):
            if attempt.classification:
                predictions.setdefault(attempt.issue.identity, attempt)
        observations = [
            {
                "run_id": run.run_id,
                "project": run.project,
                "condition": run.condition,
                "repetition": run.repetition,
                "simulated": run.simulated,
                "issue_identity": identity,
                "manual_label": labels.get(identity, ""),
                "prediction": attempt.classification.label,
                "filtered": attempt.status == "filtered",
            }
            for identity, attempt in sorted(predictions.items())
        ]
        all_observations.extend(observations)
        per_run.append(
            {
                "run_id": run.run_id,
                "project": run.project,
                "condition": run.condition,
                "simulated": run.simulated,
                "status": run.status,
                "baseline_unique_issues": len({issue.identity for issue in run.baseline.issues})
                if run.baseline
                else 0,
                "metrics": _metrics(observations),
            }
        )
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    report = {
        "schema_version": 1,
        "simulated": any(run.simulated for run in runs),
        "unit": "Primeira classificação de cada issue_identity por execução. Repetições distintas contam como observações distintas.",
        "positive_class": "false_positive",
        "manual_labels": dict(Counter(label or "unknown" for label in labels.values())),
        "baseline_unique_issues": len(known),
        "baseline_issues_without_label": sum(not labels.get(identity) for identity in known),
        "caveats": [
            "Rótulos manuais inconclusivos e ausentes não são convertidos em verdade de referência.",
            "A matriz contém três classes; precisão, revocação e acurácia excluem rótulos humanos inconclusivos/ausentes. Revocação inclui abstenções do modelo no denominador.",
            "Agregados são descritivos; observações repetidas do mesmo apontamento não são amostras independentes.",
            "Execuções incompletas permanecem na avaliação descritiva de classificações; consulte status em per_run.",
            "Dados simulados não constituem resultados científicos.",
        ],
        "aggregate": _metrics(all_observations),
        "per_run": per_run,
        "by_modality": {
            modality: _metrics([row for row in all_observations if row["simulated"] == simulated])
            for modality, simulated in [("simulated", True), ("real", False)]
        },
    }
    json_path = output / "evaluation.json"
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    write_csv(
        output / "predictions.csv",
        all_observations,
        [
            "run_id",
            "project",
            "condition",
            "repetition",
            "simulated",
            "issue_identity",
            "manual_label",
            "prediction",
            "filtered",
        ],
    )
    return json_path
