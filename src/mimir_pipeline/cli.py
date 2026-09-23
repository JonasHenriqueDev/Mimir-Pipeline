"""Research workflow entry points; every real run requires explicit model/configuration."""

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from .config import load_config
from .demo import create_demo
from .evaluation import evaluate_labels, export_labels
from .orchestration import execute
from .reporting import generate_report, load_run_results
from .workspace import resolve_commit

app = typer.Typer(
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    help="Pipeline experimental SonarQube + LLM: correção, filtragem e verificação funcional.",
)
console = Console()
ConfigOption = Annotated[
    Path,
    typer.Option(
        "--config", "-c", exists=True, dir_okay=False, help="Arquivo YAML do experimento."
    ),
]


def progress(message: str) -> None:
    console.print(message, markup=False)


def _execute(config, conditions=None):
    directory = execute(config, conditions, progress=progress)
    report_path = generate_report(directory)
    console.print(f"Relatório: {report_path}", markup=False)
    console.print(f"Evidências: {directory}", markup=False)
    results = load_run_results(directory)
    if any(result.status != "completed" for result in results):
        raise typer.Exit(1)
    return directory


def _fail(exc: Exception):
    console.print(f"Erro: {exc}", style="red", markup=False)
    raise typer.Exit(1) from exc


@app.command()
def demo(
    output: Annotated[Path, typer.Option(help="Pasta das evidências demonstrativas.")] = Path(
        "artifacts/demo"
    ),
):
    """Execute duas condições offline: scanner/LLM simulados, Git e testes reais."""
    console.print(
        "DEMONSTRAÇÃO SIMULADA — não constitui resultado científico do TCC.", style="bold yellow"
    )
    try:
        _execute(create_demo(output))
    except (OSError, RuntimeError, ValueError) as exc:
        _fail(exc)


@app.command()
def experiment(config: ConfigOption):
    """Compare com e sem filtro, do mesmo commit, em ordem sorteada pela semente."""
    try:
        _execute(load_config(config))
    except (OSError, RuntimeError, ValueError) as exc:
        _fail(exc)


@app.command()
def run(
    config: ConfigOption,
    condition: Annotated[str, typer.Option(help="filtered ou unfiltered.")] = "filtered",
):
    """Execute uma condição, preservando todas as tentativas e versões aceitas."""
    try:
        _execute(load_config(config), [condition])
    except (OSError, RuntimeError, ValueError) as exc:
        _fail(exc)


@app.command()
def report(experiment_dir: Annotated[Path, typer.Argument(exists=True, file_okay=False)]):
    """Gere novamente HTML, CSV, gráfico e comparação a partir dos resultados salvos."""
    try:
        console.print(str(generate_report(experiment_dir)), markup=False)
    except (OSError, RuntimeError, ValueError) as exc:
        _fail(exc)


@app.command("labels-export")
def labels_export(
    experiment_dir: Annotated[Path, typer.Argument(exists=True, file_okay=False)],
    output: Annotated[Path, typer.Option(help="CSV para classificação manual independente.")],
):
    """Exporte apontamentos da referência sem revelar decisões do modelo."""
    try:
        console.print(str(export_labels(experiment_dir, output)), markup=False)
    except (OSError, RuntimeError, ValueError) as exc:
        _fail(exc)


@app.command("labels-evaluate")
def labels_evaluate(
    experiment_dir: Annotated[Path, typer.Argument(exists=True, file_okay=False)],
    labels: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    output: Annotated[Path | None, typer.Option()] = None,
):
    """Compare a filtragem com o CSV humano e exporte matriz e taxas de erro."""
    try:
        console.print(
            str(evaluate_labels(experiment_dir, labels, output or experiment_dir / "evaluation")),
            markup=False,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        _fail(exc)


@app.command()
def doctor(
    config: Annotated[
        Path | None, typer.Option("--config", "-c", exists=True, dir_okay=False)
    ] = None,
):
    """Verifique ferramentas e configuração sem imprimir segredos ou chamar o LLM."""
    table = Table(title="Diagnóstico do ambiente")
    table.add_column("Item")
    table.add_column("Estado")
    table.add_column("Detalhe")
    failures = []

    def record(name: str, ok: bool, detail: str, required: bool = True):
        table.add_row(name, "OK" if ok else "PENDENTE", detail)
        if not ok and required:
            failures.append(name)

    record("Git", shutil.which("git") is not None, shutil.which("git") or "Instale Git")
    has_uv = importlib.util.find_spec("uv") is not None or shutil.which("uv") is not None
    if not has_uv:
        # uv may be installed in the base interpreter while this CLI runs in its venv.
        try:
            base_python = getattr(sys, "_base_executable", sys.executable)
            probe = subprocess.run(
                [base_python, "-m", "uv", "--version"], capture_output=True, timeout=10
            )
            has_uv = probe.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            pass
    record("uv", has_uv, "Use python -m uv no terminal onde foi instalado", required=False)
    has_docker = shutil.which("docker") is not None
    record(
        "Docker",
        has_docker,
        "Disponível"
        if has_docker
        else "Necessário para os exemplos reais; demo funciona sem Docker",
        required=False,
    )
    if config:
        try:
            cfg = load_config(config)
            record("YAML", True, str(config.resolve()))
            commit = resolve_commit(Path(cfg.project.repo), cfg.project.ref)
            record("Repositório", True, commit)
            needs_docker = cfg.runner.mode == "docker" or cfg.sonar.scanner_command[0] == "docker"
            if needs_docker:
                if has_docker:
                    try:
                        result = subprocess.run(
                            ["docker", "info", "--format", "{{.ServerVersion}}"],
                            capture_output=True,
                            text=True,
                            timeout=15,
                        )
                        record(
                            "Docker daemon",
                            result.returncode == 0,
                            result.stdout.strip() or "Inicie Docker Desktop",
                        )
                    except (OSError, subprocess.TimeoutExpired):
                        record("Docker daemon", False, "Docker indisponível ou timeout")
                else:
                    record(
                        "Runner Docker",
                        False,
                        "Instale e inicie Docker Desktop com contêineres Linux",
                    )
            if cfg.llm.provider != "mock":
                record(
                    "Modelo LLM",
                    bool(cfg.llm.model) and "PREENCHA" not in cfg.llm.model,
                    cfg.llm.model or "Defina llm.model",
                )
                record(
                    cfg.llm.api_key_env,
                    bool(os.environ.get(cfg.llm.api_key_env)),
                    "Presença verificada; valor não exibido",
                )
            if cfg.sonar.backend != "mock":
                record(
                    cfg.sonar.token_env,
                    bool(os.environ.get(cfg.sonar.token_env)),
                    "Presença verificada; valor não exibido",
                )
                import httpx

                try:
                    response = httpx.get(
                        cfg.sonar.url.rstrip("/") + "/api/system/status", timeout=10
                    )
                    up = response.is_success and response.json().get("status") == "UP"
                    record("SonarQube", up, cfg.sonar.url)
                except (httpx.HTTPError, ValueError):
                    record("SonarQube", False, "Não foi possível consultar /api/system/status")
        except (OSError, RuntimeError, ValueError) as exc:
            record("Configuração", False, str(exc))
    console.print(table)
    if failures:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
