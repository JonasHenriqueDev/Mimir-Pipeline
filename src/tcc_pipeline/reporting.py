"""Portable experiment reports. Incomplete or incomparable runs never form a pair."""

import base64
import csv
import html
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import RunResult


def load_run_results(experiment_dir: Path) -> list[RunResult]:
    directory = Path(experiment_dir)
    # Never interpret result.json fixtures inside the evaluated repository as run data.
    if (directory / "runs").is_dir():
        paths = sorted((directory / "runs").glob("*/result.json"))
    elif (directory / "result.json").is_file():
        paths = [directory / "result.json"]
    else:
        paths = sorted(directory.glob("*/result.json"))
    if not paths:
        raise ValueError(f"Nenhum result.json encontrado em {experiment_dir}.")
    results = []
    seen = set()
    for path in paths:
        result = RunResult.model_validate_json(path.read_text(encoding="utf-8"))
        if result.run_id in seen:
            raise ValueError(f"run_id duplicado: {result.run_id}")
        seen.add(result.run_id)
        results.append(result)
    return results


def _duration(run: RunResult) -> float | None:
    if not run.started_at or not run.ended_at:
        return None
    try:
        return max(
            0,
            (
                datetime.fromisoformat(run.ended_at) - datetime.fromisoformat(run.started_at)
            ).total_seconds(),
        )
    except ValueError:
        return None


def summarize_run(run: RunResult) -> dict[str, Any]:
    initial = Counter(issue.identity for issue in run.baseline.issues) if run.baseline else None
    final = Counter(issue.identity for issue in run.final.issues) if run.final else None
    observed = initial is not None and final is not None
    statuses = Counter(attempt.status for attempt in run.attempts)
    usage = run.llm_usage
    input_tokens, output_tokens = usage.get("input_tokens"), usage.get("output_tokens")
    total_tokens = usage.get("total_tokens")
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
    return {
        "run_id": run.run_id,
        "experiment_id": run.experiment_id,
        "project": run.project,
        "condition": run.condition,
        "repetition": run.repetition,
        "base_commit": run.base_commit,
        "status": run.status,
        "stop_reason": run.stop_reason,
        "simulated": run.simulated,
        "initial_issues": sum(initial.values()) if initial is not None else None,
        "final_issues": sum(final.values()) if final is not None else None,
        "resolved_issues": sum((initial - final).values()) if observed else None,
        "new_issues": sum((final - initial).values()) if observed else None,
        "net_reduction": sum(initial.values()) - sum(final.values()) if observed else None,
        "attempts": len(run.attempts),
        "accepted": statuses["accepted"],
        "rejected": statuses["rejected"],
        "filtered": statuses["filtered"],
        "validation_failures": statuses["validation_failed"],
        "infrastructure_errors": statuses["infra_error"],
        "llm_errors": statuses["llm_error"],
        "run_failed": run.status == "failed",
        "duration_seconds": _duration(run),
        "llm_calls": usage.get("calls", 0),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cost_usd": usage.get("cost_usd"),
        "llm_latency_seconds": usage.get("latency_seconds"),
        "initial_effort_minutes": sum(issue.effort_minutes for issue in run.baseline.issues)
        if run.baseline
        else None,
        "final_effort_minutes": sum(issue.effort_minutes for issue in run.final.issues)
        if run.final
        else None,
        "initial_severity": dict(Counter(issue.severity for issue in run.baseline.issues))
        if run.baseline
        else {},
        "final_severity": dict(Counter(issue.severity for issue in run.final.issues))
        if run.final
        else {},
    }


def _baseline_signature(run: RunResult) -> dict[str, Any] | None:
    if run.baseline is None:
        return None
    metadata = run.baseline.metadata
    profiles = metadata.get("quality_profiles", {}).get("profiles", [])
    plugins = metadata.get("plugins", {}).get("plugins", [])
    return {
        "issues": sorted(
            (issue.identity, issue.severity, issue.effort_minutes) for issue in run.baseline.issues
        ),
        "metrics": run.baseline.metrics,
        "server_version": metadata.get("server_version"),
        "mode": metadata.get("mode"),
        "profiles": sorted(
            (
                str(profile.get("language")),
                str(profile.get("key")),
                str(profile.get("rulesUpdatedAt")),
                profile.get("activeRuleCount"),
            )
            for profile in profiles
        ),
        "plugins": sorted(
            (str(plugin.get("key")), str(plugin.get("version"))) for plugin in plugins
        ),
    }


def compare_runs(runs: list[RunResult]) -> dict[str, Any]:
    groups = defaultdict(list)
    for run in runs:
        groups[(run.experiment_id, run.project, run.repetition, run.base_commit)].append(run)
    pairs, excluded = [], []
    metrics = [
        "resolved_issues",
        "new_issues",
        "net_reduction",
        "accepted",
        "rejected",
        "filtered",
        "validation_failures",
        "infrastructure_errors",
        "llm_errors",
        "duration_seconds",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cost_usd",
        "initial_effort_minutes",
        "final_effort_minutes",
    ]
    for key, group in sorted(groups.items()):
        entry = dict(zip(["experiment_id", "project", "repetition", "base_commit"], key))
        entry["run_ids"] = [run.run_id for run in group]
        by_condition = defaultdict(list)
        for run in group:
            by_condition[run.condition].append(run)
        if any(len(by_condition[condition]) != 1 for condition in ["filtered", "unfiltered"]):
            excluded.append({**entry, "reason": "missing_or_duplicate_arm"})
            continue
        filtered, unfiltered = by_condition["filtered"][0], by_condition["unfiltered"][0]
        if any(
            run.status != "completed" or run.baseline is None or run.final is None
            for run in [filtered, unfiltered]
        ):
            excluded.append({**entry, "reason": "incomplete_arm"})
            continue
        if filtered.simulated != unfiltered.simulated:
            excluded.append({**entry, "reason": "mixed_simulation"})
            continue
        if _baseline_signature(filtered) != _baseline_signature(unfiltered):
            excluded.append({**entry, "reason": "incomparable_baseline"})
            continue
        left, right = summarize_run(filtered), summarize_run(unfiltered)
        deltas = {
            metric: left[metric] - right[metric]
            if left[metric] is not None and right[metric] is not None
            else None
            for metric in metrics
        }
        pairs.append(
            {
                **entry,
                "simulated": filtered.simulated,
                "baseline_comparable": True,
                "filtered": left,
                "unfiltered": right,
                "delta_filtered_minus_unfiltered": deltas,
            }
        )
    return {
        "schema_version": 1,
        "delta_definition": "filtered minus unfiltered",
        "baseline_check": "Mesmo experimento, projeto, repetição, commit, modalidade, identidades/severidades/esforços e métricas iniciais; versões, modo e perfis quando disponíveis.",
        "caveat": "Comparabilidade da configuração, perfil de regras e ambiente também exige revisão dos manifests; isto não constitui prova de equivalência semântica ou teste de significância.",
        "pairs": pairs,
        "excluded": excluded,
    }


def _csv_cell(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    # A CSV opened in a spreadsheet must not execute source-derived formulas.
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + value
    return value


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _csv_cell(row.get(key)) for key in fields})


def _display(value: Any) -> str:
    if value is None:
        return "n/d"
    if isinstance(value, float):
        return f"{value:,.4f}".rstrip("0").rstrip(".")
    if isinstance(value, dict):
        return ", ".join(f"{key}: {value[key]}" for key in sorted(value)) or "—"
    return str(value)


def _table(rows: list[dict[str, Any]], columns: list[tuple[str, str]]) -> str:
    header = "".join(f"<th>{html.escape(label)}</th>" for _, label in columns)
    body = "".join(
        "<tr>"
        + "".join(f"<td>{html.escape(_display(row.get(key)))}</td>" for key, _ in columns)
        + "</tr>"
        for row in rows
    )
    return (
        f'<div class="scroll"><table><thead><tr>{header}</tr></thead><tbody>{body}</tbody></table></div>'
        if rows
        else '<p class="muted">Nenhum registro.</p>'
    )


def _chart(rows: list[dict[str, Any]], path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    displayed = [
        row for row in rows if row["initial_issues"] is not None and row["final_issues"] is not None
    ][:20]
    fig, ax = plt.subplots(figsize=(max(7, min(14, len(displayed) * 1.1)), 4.5))
    fig.set_facecolor("#f3f6fc")
    ax.set_facecolor("#f3f6fc")
    if displayed:
        positions = list(range(len(displayed)))
        ax.bar(
            [position - 0.18 for position in positions],
            [row["initial_issues"] for row in displayed],
            width=0.36,
            color="#94a3b8",
            label="Inicial",
        )
        ax.bar(
            [position + 0.18 for position in positions],
            [row["final_issues"] for row in displayed],
            width=0.36,
            color="#2563eb",
            label="Final",
        )
        ax.set_xticks(
            positions,
            [f"{row['project']}\n{row['condition']} · r{row['repetition']}" for row in displayed],
            rotation=25,
            ha="right",
        )
        ax.legend(frameon=False)
        ax.set_ylabel("Apontamentos observados")
        ax.yaxis.get_major_locator().set_params(integer=True)
    else:
        ax.text(
            0.5,
            0.5,
            "Sem snapshots inicial/final para comparação",
            transform=ax.transAxes,
            ha="center",
        )
        ax.set_axis_off()
    ax.set_title(
        "DADOS SIMULADOS · demonstração"
        if any(row["simulated"] for row in rows)
        else "Apontamentos por execução",
        loc="left",
        fontweight="bold",
        pad=18,
    )
    for edge in ["top", "right"]:
        ax.spines[edge].set_visible(False)
    fig.tight_layout()
    fig.savefig(path, dpi=160, facecolor=fig.get_facecolor())
    plt.close(fig)


def generate_report(experiment_dir: Path) -> Path:
    """Write HTML, CSV, JSON and PNG in experiment_dir; return the HTML path."""
    directory = Path(experiment_dir).resolve()
    runs = load_run_results(directory)
    rows = [summarize_run(run) for run in runs]
    comparison = compare_runs(runs)
    write_csv(directory / "summary.csv", rows, list(rows[0]))
    attempts = [
        {
            "run_id": run.run_id,
            "project": run.project,
            "condition": run.condition,
            "repetition": run.repetition,
            "simulated": run.simulated,
            "attempt": attempt.index,
            "iteration": attempt.iteration,
            "issue_identity": attempt.issue.identity,
            "rule": attempt.issue.rule,
            "path": attempt.issue.path,
            "severity": attempt.issue.severity,
            "status": attempt.status,
            "reason": attempt.reason,
            "classification": attempt.classification.label if attempt.classification else "",
            "rationale": attempt.classification.rationale if attempt.classification else "",
            "command_failures": sum(
                command.returncode != 0 or command.timed_out for command in attempt.commands
            ),
            "commit": attempt.commit or "",
        }
        for run in runs
        for attempt in run.attempts
    ]
    write_csv(
        directory / "attempts.csv",
        attempts,
        [
            "run_id",
            "project",
            "condition",
            "repetition",
            "simulated",
            "attempt",
            "iteration",
            "issue_identity",
            "rule",
            "path",
            "severity",
            "status",
            "reason",
            "classification",
            "rationale",
            "command_failures",
            "commit",
        ],
    )
    (directory / "comparison.json").write_text(
        json.dumps(comparison, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    _chart(rows, directory / "chart.png")
    chart = base64.b64encode((directory / "chart.png").read_bytes()).decode("ascii")
    simulated = any(run.simulated for run in runs)
    banner = (
        '<div class="notice"><strong>DADOS SIMULADOS</strong> · Demonstração técnica. Não são resultados científicos nem evidência da eficácia de um modelo.</div>'
        if simulated
        else '<div class="notice real">Execuções reais registradas · verifique os manifests e os critérios do estudo antes de interpretar os resultados.</div>'
    )
    run_table = _table(
        rows,
        [
            ("project", "Projeto"),
            ("condition", "Condição"),
            ("repetition", "Rep."),
            ("status", "Estado"),
            ("initial_issues", "Inicial"),
            ("final_issues", "Final"),
            ("resolved_issues", "Resolvidos"),
            ("new_issues", "Novos"),
            ("net_reduction", "Redução líquida"),
            ("accepted", "Aceitos"),
            ("rejected", "Rejeitados"),
            ("filtered", "Filtrados"),
            ("validation_failures", "Falhas validação"),
            ("infrastructure_errors", "Erros infra"),
            ("llm_errors", "Erros LLM"),
        ],
    )
    resource_table = _table(
        rows,
        [
            ("run_id", "Execução"),
            ("duration_seconds", "Tempo (s)"),
            ("llm_calls", "Chamadas"),
            ("total_tokens", "Tokens"),
            ("cost_usd", "Custo (USD)"),
            ("initial_effort_minutes", "Esforço inicial (min)"),
            ("final_effort_minutes", "Esforço final (min)"),
            ("initial_severity", "Severidade inicial"),
            ("final_severity", "Severidade final"),
        ],
    )
    pair_rows = [
        {
            "project": pair["project"],
            "repetition": pair["repetition"],
            **pair["delta_filtered_minus_unfiltered"],
        }
        for pair in comparison["pairs"]
    ]
    pair_table = _table(
        pair_rows,
        [
            ("project", "Projeto"),
            ("repetition", "Rep."),
            ("net_reduction", "Δ redução líquida"),
            ("validation_failures", "Δ falhas validação"),
            ("total_tokens", "Δ tokens"),
            ("duration_seconds", "Δ tempo (s)"),
            ("cost_usd", "Δ custo (USD)"),
        ],
    )
    excluded_table = _table(
        comparison["excluded"],
        [("project", "Projeto"), ("repetition", "Rep."), ("reason", "Motivo da exclusão")],
    )
    audit_table = _table(
        [
            {
                "run_id": run.run_id,
                "base_commit": run.base_commit,
                "stop_reason": run.stop_reason,
                "error": run.error or "",
            }
            for run in runs
        ],
        [
            ("run_id", "Execução"),
            ("base_commit", "Commit de origem"),
            ("stop_reason", "Parada"),
            ("error", "Erro da execução"),
        ],
    )
    output = directory / "report.html"
    output.write_text(
        f"""<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Relatório · TCC Pipeline</title>
<style>*{{box-sizing:border-box}}body{{margin:0;background:#f3f6fc;color:#172033;font:15px/1.6 system-ui,Segoe UI,sans-serif}}main{{max-width:1440px;margin:auto;padding:44px 28px}}header{{border-bottom:1px solid #d9e2f1;padding-bottom:24px}}.eyebrow{{font-size:12px;letter-spacing:2px;font-weight:700;color:#2563eb}}h1{{font-size:38px;line-height:1.15;margin:10px 0}}h2{{font-size:21px;margin:0 0 12px}}p{{margin:8px 0}}.muted{{color:#59677f}}.notice{{margin:24px 0;background:#fff1c7;border-left:5px solid #c68b00;padding:16px 20px;border-radius:8px}}.real{{background:#e5efff;border-color:#2563eb}}.cards{{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin:24px 0}}.card,section{{background:white;border:1px solid #e0e7f1;border-radius:14px;padding:24px}}.card b{{display:block;font-size:32px;line-height:1.3}}.card span{{color:#59677f}}section{{margin-top:20px}}.scroll{{overflow:auto}}table{{border-collapse:collapse;width:100%;font-size:13px}}th,td{{padding:11px 13px;text-align:left;border-bottom:1px solid #e6ebf3;vertical-align:top}}th{{color:#52617a;font-size:11px;text-transform:uppercase;letter-spacing:.6px;background:#f7f9fc;white-space:nowrap}}td{{max-width:480px;overflow-wrap:anywhere}}tbody tr:hover{{background:#f7faff}}img{{width:100%;height:auto}}a{{color:#1d4ed8}}footer{{margin:26px 0;color:#59677f;font-size:13px}}@media(max-width:650px){{main{{padding:24px 14px}}h1{{font-size:29px}}.cards{{grid-template-columns:1fr}}section{{padding:16px}}}}</style></head>
<body><main><header><div class="eyebrow">TCC PIPELINE / EVIDÊNCIAS DO EXPERIMENTO</div><h1>Correções, verificações e custos</h1><p class="muted">Comparação entre execução com filtragem e sem filtragem de apontamentos.</p></header>
{banner}<div class="cards"><div class="card"><b>{len(runs)}</b><span>execuções registradas</span></div><div class="card"><b>{len(comparison["pairs"])}</b><span>pares completos e comparáveis</span></div><div class="card"><b>{sum(row["accepted"] for row in rows)}</b><span>patches aceitos na validação</span></div></div>
<section><h2>Apontamentos antes e depois</h2><img alt="Gráfico de apontamentos iniciais e finais por execução" src="data:image/png;base64,{chart}"><p class="muted">O gráfico mostra até 20 execuções com ambos os snapshots. Execuções incompletas podem conter somente o último estado observado.</p></section>
<section><h2>Resultados por execução</h2>{run_table}<p class="muted">Resolvidos = identidades iniciais ausentes ao final; novos = identidades adicionais; redução líquida = inicial − final. Contagens respeitam multiplicidade. Filtrar um apontamento não o remove da análise estática.</p></section>
<section><h2>Comparação pareada</h2><p>Δ = com filtragem − sem filtragem. Redução líquida positiva favorece a filtragem; tempo, custo e falhas menores favorecem a filtragem. Nenhum teste de significância foi aplicado automaticamente.</p>{pair_table}<h2 style="margin-top:22px">Pares não incluídos</h2>{excluded_table}</section>
<section><h2>Recursos, esforço e severidade</h2>{resource_table}<p class="muted">n/d indica dado indisponível. Custos são estimativas baseadas nos preços configurados, não valores de faturamento. Esforço é a estimativa do analisador, não tempo humano economizado.</p></section>
<section><h2>Rastreabilidade</h2>{audit_table}<p><a href="summary.csv">Resumo CSV</a> · <a href="attempts.csv">Tentativas CSV</a> · <a href="comparison.json">Comparação JSON</a> · <a href="chart.png">Gráfico PNG</a></p></section>
<section><h2>Limites de interpretação</h2><p>Testes aprovados e redução de apontamentos não garantem correção semântica. As métricas de classificação exigem rótulos humanos independentes, exportados sem as previsões do modelo. Falhas de validação, erros de infraestrutura e erros do LLM são apresentados separadamente.</p><p>O pareamento verifica commit, repetição, modalidade simulada e dados iniciais. Revise também configurações, versões, prompts e perfil de regras nos manifests. Resultados de regras selecionadas não representam necessariamente toda a dívida técnica do projeto.</p></section>
<footer>Relatório local, sem scripts, fontes externas ou dependências de rede. Os dados textuais são escapados para exibição.</footer></main></body></html>""",
        encoding="utf-8",
    )
    return output
