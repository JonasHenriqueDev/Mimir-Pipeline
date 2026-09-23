"""Explicit experimental state machine with independently validated candidates."""

import hashlib
import importlib.metadata
import platform
import random
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Callable

from .config import PipelineConfig
from .context import build_context
from .llm import BudgetExceeded, LLMDeadlineExceeded, LLMError, make_llm
from .models import Attempt, Issue, RunResult, Snapshot
from .patching import PatchError, apply_proposal, matches, safe_path
from .runner import Runner, RunnerError
from .sonar import SonarDeadlineExceeded, make_analyzer
from .storage import event, save_result, utc_now, write_json
from .tracking import IssueTracker
from .workspace import Workspace, git, resolve_commit, tracked_changes


class RunLimit(RuntimeError):
    pass


def fingerprint(snapshot: Snapshot) -> Counter:
    return Counter(issue.identity for issue in snapshot.issues)


def acceptance_reason(
    before: Snapshot, after: Snapshot, target: Issue, reject_new: bool
) -> str | None:
    if any(issue.key == target.key for issue in after.issues):
        return "Apontamento alvo permanece na reanálise"
    if fingerprint(after)[target.identity] >= fingerprint(before)[target.identity]:
        return "A quantidade de apontamentos com a identidade do alvo não diminuiu"
    if reject_new and fingerprint(after) - fingerprint(before):
        return "A reanálise detectou novos apontamentos de manutenibilidade"
    return None


def project_environment() -> dict:
    versions = {}
    for name in ["mimir-pipeline", "httpx", "pydantic", "pyyaml", "typer"]:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = "source"
    source_hash = hashlib.sha256()
    package_dir = Path(__file__).parent
    for source in sorted(package_dir.glob("*.py")):
        source_hash.update(source.name.encode())
        source_hash.update(source.read_bytes())
    lockfile = package_dir.parent.parent / "uv.lock"
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "packages": versions,
        "pipeline_source_sha256": source_hash.hexdigest(),
        "uv_lock_sha256": hashlib.sha256(lockfile.read_bytes()).hexdigest()
        if lockfile.is_file()
        else None,
    }


def run_condition(
    config: PipelineConfig,
    workspace: Workspace,
    experiment_dir: Path,
    condition: str,
    repetition: int,
    progress: Callable[[str], None] = print,
) -> RunResult:
    run_id = f"r{repetition:02d}-{condition}"
    run_dir = experiment_dir / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    result = RunResult(
        run_id=run_id,
        experiment_id=experiment_dir.name,
        project=config.project.name,
        condition=condition,
        repetition=repetition,
        base_commit=workspace.base_commit,
        started_at=utc_now(),
        simulated=config.sonar.backend == "mock",
    )
    catalog = Path(config.output_dir) / "catalog.sqlite3"
    deadline = time.monotonic() + config.experiment.max_seconds
    commit = workspace.base_commit
    analyzer = None
    llm = None
    tracker = IssueTracker()
    write_json(run_dir / "config.json", config.model_dump(mode="json"))
    write_json(run_dir / "environment.json", project_environment())

    def checkpoint():
        result.llm_usage = dict(llm.usage) if llm is not None else {}
        save_result(run_dir, result, catalog)

    def check_time():
        if time.monotonic() >= deadline:
            raise RunLimit("max_seconds")

    def scan(repo: Path, output: Path) -> Snapshot:
        check_time()
        try:
            snapshot = analyzer.analyze(repo, output)
        except SonarDeadlineExceeded as exc:
            raise RunLimit("max_seconds") from exc
        snapshot = tracker.register(snapshot)
        write_json(output / "snapshot.json", snapshot.model_dump(mode="json"))
        return snapshot

    def validate(repo: Path, folder: Path, attempt: Attempt | None = None) -> tuple[bool, str]:
        results = []
        with Runner(config.runner, repo, folder, deadline) as runner:
            for stage, commands in [
                ("setup", config.project.setup_commands),
                ("build", config.project.build_commands),
                ("test", config.project.test_commands),
            ]:
                if stage == "build":
                    runner.finish_setup()
                for index, command in enumerate(commands, 1):
                    check_time()
                    try:
                        outcome = runner.run(command, f"{stage}-{index:02d}")
                    except RunnerError as exc:
                        check_time()
                        raise exc
                    results.append(outcome)
                    write_json(
                        folder / "commands.json", [item.model_dump(mode="json") for item in results]
                    )
                    if attempt is not None:
                        attempt.commands.append(outcome)
                    if outcome.returncode != 0 or outcome.timed_out:
                        check_time()
                        reason = f"{stage}: {'timeout' if outcome.timed_out else 'exit ' + str(outcome.returncode)}"
                        if stage == "setup":
                            raise RunnerError(reason)
                        return False, reason
        return True, ""

    try:
        checkpoint()
        current_repo = workspace.checkout(f"{run_id}-baseline", commit)
        result.worktree = str(current_repo)
        analyzer = make_analyzer(
            config.sonar,
            config.project,
            f"tcc-{config.project.name}-{experiment_dir.name}-{run_id}",
        )
        analyzer.deadline = deadline
        llm = make_llm(config.llm, run_dir / "llm")
        llm.deadline = deadline
        progress(f"{run_id}: verificando build e testes de referência")
        passed, reason = validate(current_repo, run_dir / "baseline" / "validation")
        if not passed:
            raise RunnerError("Referência funcional inválida: " + reason)
        if tracked_changes(current_repo):
            raise RunnerError("Build/testes alteraram arquivos versionados da referência")
        current = scan(current_repo, run_dir / "baseline" / "sonar")
        result.baseline = current
        result.final = current
        event(run_dir, "baseline", issues=len(current.issues), commit=commit)
        checkpoint()
        attempts_per_issue: Counter = Counter()
        filtered: set[tuple[str, str]] = set()
        result.stop_reason = "max_iterations"
        stop = False
        for iteration in range(1, config.experiment.max_iterations + 1):
            check_time()
            candidates = [
                issue
                for issue in current.issues
                if (not config.sonar.rules or issue.rule in config.sonar.rules)
                and matches(issue.path, config.project.allowed_edit_globs)
                and not matches(issue.path, config.project.protected_globs)
            ]
            ranks = {
                "BLOCKER": 0,
                "HIGH": 1,
                "CRITICAL": 1,
                "MAJOR": 2,
                "MEDIUM": 2,
                "MINOR": 3,
                "LOW": 3,
                "INFO": 4,
            }
            candidates.sort(
                key=lambda issue: (
                    ranks.get(issue.severity, 2),
                    issue.rule,
                    issue.path,
                    issue.line,
                    issue.key,
                )
            )
            if not candidates:
                result.stop_reason = "no_eligible_issues"
                break
            accepted_this_iteration = 0
            for old_issue in candidates:
                check_time()
                # Resolve against the most recent snapshot; previous fixes can shift lines.
                issue = next((item for item in current.issues if item.key == old_issue.key), None)
                if issue is None:
                    continue
                if (issue.identity, commit) in filtered:
                    continue
                if attempts_per_issue[issue.identity] >= config.experiment.max_attempts_per_issue:
                    continue
                if len(result.attempts) >= config.experiment.max_attempts:
                    result.stop_reason = "max_attempts"
                    stop = True
                    break
                number = len(result.attempts) + 1
                folder = run_dir / "attempts" / f"{number:04d}"
                folder.mkdir(parents=True)
                attempt = Attempt(
                    index=number,
                    iteration=iteration,
                    issue=issue,
                    status="running",
                    artifact_dir=str(folder),
                )
                result.attempts.append(attempt)
                attempts_per_issue[issue.identity] += 1
                event(run_dir, "attempt_started", index=number, issue=issue.key)
                checkpoint()
                progress(f"{run_id}: tentativa {number}, {issue.rule} em {issue.path}:{issue.line}")
                try:
                    context = build_context(
                        current_repo, issue, config.project, config.llm.max_context_chars
                    )
                    write_json(folder / "context.json", context)
                    if condition == "filtered":
                        check_time()
                        attempt.classification = llm.classify(issue, context)
                        if attempt.classification.label == "false_positive":
                            attempt.status = "filtered"
                            attempt.reason = attempt.classification.rationale
                            filtered.add((issue.identity, commit))
                            continue
                    check_time()
                    attempt.proposal = llm.propose(issue, context)
                    write_json(folder / "proposal.json", attempt.proposal.model_dump(mode="json"))
                    candidate = workspace.checkout(f"{run_id}-a{number:04d}", commit)
                    patch, paths = apply_proposal(
                        candidate, attempt.proposal, config.project, config.experiment
                    )
                    (folder / "patch.diff").write_text(patch, encoding="utf-8")
                    expected_bytes = {
                        path: safe_path(candidate, path).read_bytes() for path in paths
                    }
                    passed, reason = validate(candidate, folder / "validation", attempt)
                    if not passed:
                        attempt.status, attempt.reason = "validation_failed", reason
                        continue
                    if tracked_changes(candidate) != set(paths) or any(
                        safe_path(candidate, path).read_bytes() != data
                        for path, data in expected_bytes.items()
                    ):
                        raise PatchError(
                            "Build/testes modificaram os arquivos da proposta ou outros arquivos versionados"
                        )
                    updated = scan(candidate, folder / "sonar")
                    if tracked_changes(candidate) != set(paths) or any(
                        safe_path(candidate, path).read_bytes() != data
                        for path, data in expected_bytes.items()
                    ):
                        raise PatchError("Scanner modificou arquivos versionados")
                    rejection = acceptance_reason(
                        current, updated, issue, config.experiment.reject_new_issues
                    )
                    if rejection:
                        attempt.status, attempt.reason = "rejected", rejection
                        continue
                    commit = workspace.commit(
                        candidate, paths, f"TCC {run_id}: {issue.rule} attempt {number}"
                    )
                    attempt.status, attempt.commit = "accepted", commit
                    attempt.reason = (
                        "Alvo removido; build, testes e política de reanálise aprovados"
                    )
                    current, current_repo = updated, candidate
                    result.final, result.worktree = current, str(current_repo)
                    accepted_this_iteration += 1
                except PatchError as exc:
                    attempt.status, attempt.reason = "rejected", str(exc)
                except LLMDeadlineExceeded as exc:
                    attempt.status, attempt.reason = "llm_error", str(exc)
                    raise RunLimit("max_seconds") from exc
                except BudgetExceeded as exc:
                    attempt.status, attempt.reason = "llm_error", str(exc)
                    result.stop_reason, stop = "llm_budget", True
                    break
                except LLMError as exc:
                    attempt.status, attempt.reason = "llm_error", str(exc)
                    raise
                except RunLimit:
                    attempt.status, attempt.reason = "infra_error", "Tempo limite atingido"
                    raise
                except RunnerError as exc:
                    attempt.status, attempt.reason = "infra_error", str(exc)
                    raise
                except Exception as exc:
                    attempt.status, attempt.reason = "infra_error", f"{type(exc).__name__}: {exc}"
                    raise
                finally:
                    write_json(folder / "attempt.json", attempt.model_dump(mode="json"))
                    event(
                        run_dir,
                        "attempt_finished",
                        index=number,
                        status=attempt.status,
                        reason=attempt.reason,
                    )
                    checkpoint()
            if stop:
                break
            if accepted_this_iteration == 0:
                result.stop_reason = "no_progress"
                break
        # Publish the accepted code state again: last rejected scan must not become the final state.
        if time.monotonic() < deadline:
            final_snapshot = scan(current_repo, run_dir / "final" / "sonar")
            if fingerprint(final_snapshot) != fingerprint(current):
                raise RunnerError(
                    "Reanálise final diverge da última versão aceita; verificar determinismo do scanner"
                )
            result.final = final_snapshot
        else:
            result.stop_reason = "max_seconds"
        result.status = "completed"
    except RunLimit as exc:
        result.status = "completed" if result.baseline is not None else "failed"
        result.stop_reason = str(exc)
    except KeyboardInterrupt:
        result.status, result.stop_reason = "interrupted", "keyboard_interrupt"
        raise
    except Exception as exc:
        result.status, result.stop_reason = "failed", "error"
        result.error = f"{type(exc).__name__}: {exc}"
        event(run_dir, "run_failed", error=result.error)
        progress(f"{run_id}: falhou — {result.error}")
    finally:
        result.ended_at = utc_now()
        checkpoint()
        if llm is not None:
            llm.close()
        if analyzer is not None:
            analyzer.close()
    progress(f"{run_id}: {result.status} ({result.stop_reason})")
    return result


def execute(
    config: PipelineConfig,
    conditions: list[str] | None = None,
    progress: Callable[[str], None] = print,
) -> Path:
    conditions = conditions or ["filtered", "unfiltered"]
    if not conditions or any(
        condition not in {"filtered", "unfiltered"} for condition in conditions
    ):
        raise ValueError("Condição inválida")
    source = Path(config.project.repo).resolve()
    base_commit = resolve_commit(source, config.project.ref)
    if config.llm.provider != "mock" and not config.llm.model:
        raise ValueError("Defina llm.model explicitamente antes do experimento")
    output = Path(config.output_dir).resolve()
    if output.is_relative_to(source):
        raise ValueError("output_dir deve ficar fora do repositório avaliado")
    output.mkdir(parents=True, exist_ok=True)
    experiment_id = "exp-" + uuid.uuid4().hex[:12]
    directory = output / experiment_id
    directory.mkdir()
    workspace = Workspace(source, directory / "workspace", base_commit)
    order = []
    rng = random.Random(config.experiment.seed)
    for repetition in range(1, config.experiment.repetitions + 1):
        arms = list(conditions)
        rng.shuffle(arms)
        order.extend((repetition, arm) for arm in arms)
    manifest = {
        "schema_version": 1,
        "experiment_id": experiment_id,
        "created_at": utc_now(),
        "base_commit": base_commit,
        "source": str(source),
        "config": config.model_dump(mode="json"),
        "order": order,
        "source_dirty": bool(git(source, "status", "--porcelain")),
        "environment": project_environment(),
        "simulated": config.sonar.backend == "mock",
    }
    write_json(directory / "experiment.json", manifest)
    for repetition, condition in order:
        run_condition(config, workspace, directory, condition, repetition, progress)
    return directory
