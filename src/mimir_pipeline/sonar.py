"""SonarQube scanner and API adapter, with an explicitly simulated offline backend."""

from __future__ import annotations

import fnmatch
import hashlib
import html
import json
import os
import re
import signal
import subprocess
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

import httpx

from .config import ProjectConfig, SonarConfig
from .models import Issue, Snapshot
from .runner import clean_environment


class SonarError(RuntimeError):
    """The analysis failed or could not be proved fresh and complete."""


class SonarDeadlineExceeded(SonarError):
    """The experiment-wide time budget expired before analysis could finish."""


def _source_path(repo: Path, relative: str) -> Path:
    relative = relative.replace("\\", "/")
    path = PurePosixPath(relative)
    if path.is_absolute() or ".." in path.parts or ":" in relative:
        raise SonarError(f"Caminho inválido na análise: {relative}")
    target = (repo / relative).resolve()
    if not target.is_relative_to(repo.resolve()):
        raise SonarError(f"Caminho fora do repositório: {relative}")
    return target


def _anchor(repo: Path, relative: str, line: int, fallback: str) -> str:
    path = _source_path(repo, relative)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        return lines[line - 1].strip() if 0 < line <= len(lines) else fallback
    except (OSError, UnicodeError):
        return fallback


def _effort_minutes(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip()
    if not text:
        return 0.0
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return float(text)
    # Sonar's working day uses eight hours; retain this convention in exported data.
    matches = re.findall(r"(\d+(?:\.\d+)?)\s*(d|h|min)", text)
    return sum(float(amount) * {"d": 480, "h": 60, "min": 1}[unit] for amount, unit in matches)


def _save_snapshot(snapshot: Snapshot, artifacts: Path) -> Snapshot:
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "snapshot.json").write_text(snapshot.model_dump_json(indent=2), encoding="utf-8")
    return snapshot


def _kill_tree(process: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            timeout=15,
            check=False,
        )
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.kill()


class SonarAnalyzer:
    """One analysis stream per project key. Queries always follow its completed CE task."""

    def __init__(self, config: SonarConfig, project: ProjectConfig, project_key: str):
        self.config = config
        self.project = project
        self.project_key = project_key
        self.token = os.environ.get(config.token_env, "")
        if not self.token:
            raise SonarError(f"Defina a variável {config.token_env} antes de analisar.")
        self.client = httpx.Client(
            base_url=config.url.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=min(config.timeout_seconds, 60),
            follow_redirects=False,
        )
        self._last_task_id: str | None = None
        self.deadline: float | None = None

    def close(self) -> None:
        self.client.close()

    def _redact(self, value: str) -> str:
        return value.replace(self.token, "[REDACTED]") if self.token else value

    def _timeout(self, maximum: float) -> float:
        remaining = (
            maximum if self.deadline is None else min(maximum, self.deadline - time.monotonic())
        )
        if remaining <= 0:
            raise SonarDeadlineExceeded(
                "Limite de tempo do experimento atingido durante a análise."
            )
        return remaining

    def _get(self, endpoint: str, *, request_timeout: float = 60, **params: Any) -> dict[str, Any]:
        try:
            response = self.client.get(
                endpoint,
                params=params,
                timeout=self._timeout(min(self.config.timeout_seconds, request_timeout)),
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("A resposta não é um objeto JSON.")
            return payload
        except (httpx.HTTPError, ValueError) as exc:
            raise SonarError(self._redact(f"SonarQube {endpoint}: {exc}")) from exc

    def _scanner(self, repo: Path, artifacts: Path) -> dict[str, str]:
        metadata = _source_path(repo, ".scannerwork/report-task.txt")
        # Never reuse an old task when a scanner exits without publishing a report.
        if metadata.exists():
            metadata.unlink()
        scanner_url = getattr(self.config, "scanner_url", None) or self.config.url
        variables = {
            "repo": str(repo.resolve()),
            "project_key": self.project_key,
            "sonar_url": scanner_url,
        }
        command = []
        for argument in self.config.scanner_command:
            for key, value in variables.items():
                argument = argument.replace("{" + key + "}", value)
            command.append(argument)
        if not command:
            raise SonarError("scanner_command está vazio.")
        if any(
            "sonar.token=" in arg or "sonar.login=" in arg or "sonar.password=" in arg
            for arg in command
        ):
            raise SonarError("Credenciais do scanner devem vir de SONAR_TOKEN, nunca do comando.")
        forbidden = {
            "sonar.token",
            "sonar.login",
            "sonar.password",
            "sonar.projectKey",
            "sonar.host.url",
            "sonar.scanner.metadataFilePath",
        }
        if forbidden.intersection(self.config.properties):
            raise SonarError(
                "properties contém autenticação ou propriedades controladas pela pipeline."
            )
        properties = {
            **self.config.properties,
            "sonar.projectKey": self.project_key,
            "sonar.host.url": scanner_url,
            "sonar.scanner.metadataFilePath": ".scannerwork/report-task.txt",
        }
        command.extend(f"-D{key}={value}" for key, value in properties.items())
        container_name = None
        # A timed-out docker client does not necessarily stop its container.
        if Path(command[0]).stem.lower() == "docker" and len(command) > 1 and command[1] == "run":
            if any(arg == "--name" or arg.startswith("--name=") for arg in command):
                raise SonarError(
                    "A pipeline atribui o nome do contêiner do scanner; retire --name."
                )
            container_name = "tcc-scan-" + uuid.uuid4().hex[:12]
            command[2:2] = ["--name", container_name]
        artifacts.mkdir(parents=True, exist_ok=True)
        (artifacts / "scanner-command.json").write_text(
            self._redact(json.dumps(command, ensure_ascii=False, indent=2)), encoding="utf-8"
        )
        # The scanner needs only its own credential, not the LLM or cloud keys.
        environment = clean_environment()
        environment["SONAR_TOKEN"] = self.token
        environment["SONAR_HOST_URL"] = scanner_url
        scanner_timeout = self._timeout(self.config.timeout_seconds)
        started = time.monotonic()
        process = None
        try:
            process = subprocess.Popen(
                command,
                cwd=repo,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                start_new_session=os.name != "nt",
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
            )
            try:
                output, _ = process.communicate(timeout=scanner_timeout)
            except subprocess.TimeoutExpired as exc:
                _kill_tree(process)
                output, _ = process.communicate(timeout=15)
                (artifacts / "scanner.log").write_text(self._redact(output), encoding="utf-8")
                self._timeout(self.config.timeout_seconds)
                raise SonarError("SonarScanner excedeu o limite de tempo.") from exc
            (artifacts / "scanner.log").write_text(self._redact(output), encoding="utf-8")
            if process.returncode:
                raise SonarError(
                    f"SonarScanner retornou código {process.returncode}; veja scanner.log."
                )
        except OSError as exc:
            raise SonarError(self._redact(f"Não foi possível executar o scanner: {exc}")) from exc
        except KeyboardInterrupt:
            if process is not None and process.poll() is None:
                _kill_tree(process)
                output, _ = process.communicate(timeout=15)
                (artifacts / "scanner.log").write_text(self._redact(output), encoding="utf-8")
            raise
        finally:
            if container_name:
                try:
                    subprocess.run(
                        [command[0], "rm", "-f", container_name],
                        capture_output=True,
                        timeout=20,
                        check=False,
                    )
                except (OSError, subprocess.TimeoutExpired):
                    # Preserve the scanner failure; Docker cleanup is best effort if its daemon died.
                    pass
            (artifacts / "scanner-duration.json").write_text(
                json.dumps({"seconds": time.monotonic() - started}), encoding="utf-8"
            )
        if not metadata.is_file():
            raise SonarError("O scanner não gerou um novo .scannerwork/report-task.txt.")
        task = dict(
            line.split("=", 1)
            for line in metadata.read_text(encoding="utf-8").splitlines()
            if "=" in line and not line.lstrip().startswith("#")
        )
        if task.get("projectKey") != self.project_key or not task.get("ceTaskId"):
            raise SonarError(
                "O relatório do scanner não pertence ao projeto esperado ou não contém ceTaskId."
            )
        if task["ceTaskId"] == self._last_task_id:
            raise SonarError("O scanner reutilizou uma tarefa de análise antiga.")
        self._last_task_id = task["ceTaskId"]
        (artifacts / "report-task.json").write_text(json.dumps(task, indent=2), encoding="utf-8")
        return task

    def _wait_for_analysis(self, task_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + self.config.timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SonarError("Tempo esgotado aguardando o processamento no SonarQube.")
            task = self._get("api/ce/task", id=task_id, request_timeout=remaining).get("task", {})
            if task.get("id") != task_id or task.get("componentKey") != self.project_key:
                raise SonarError(
                    "A tarefa do servidor não corresponde ao projeto e à tarefa solicitados."
                )
            state = task.get("status")
            if state == "SUCCESS":
                if not task.get("analysisId"):
                    raise SonarError("Tarefa concluída sem analysisId.")
                return task
            if state in {"FAILED", "CANCELED"}:
                raise SonarError(self._redact(f"Análise {state}: {task.get('errorMessage', '')}"))
            if state not in {"PENDING", "IN_PROGRESS"}:
                raise SonarError(f"Estado inesperado da tarefa de análise: {state}")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SonarError("Tempo esgotado aguardando o processamento no SonarQube.")
            time.sleep(self._timeout(min(self.config.poll_seconds, remaining)))

    def _assert_latest(self, analysis_id: str) -> None:
        analyses = self._get("api/project_analyses/search", project=self.project_key, ps=1).get(
            "analyses", []
        )
        if not analyses or analyses[0].get("key") != analysis_id:
            raise SonarError(
                "A análise mais recente foi substituída ou ainda não está disponível; snapshot rejeitado."
            )

    def _issues(self, repo: Path, artifacts: Path) -> list[Issue]:
        collected: list[dict[str, Any]] = []
        components: dict[str, dict[str, Any]] = {}
        page = 1
        total = None
        selector = (
            {"types": "CODE_SMELL"}
            if self.config.mode == "standard"
            else {"impactSoftwareQualities": "MAINTAINABILITY"}
        )
        while True:
            payload = self._get(
                "api/issues/search",
                components=self.project_key,
                resolved="false",
                p=page,
                ps=500,
                **selector,
            )
            reported = int(payload.get("paging", {}).get("total", payload.get("total", -1)))
            if reported < 0:
                raise SonarError(
                    "A consulta de issues não informou seu total; completude não verificável."
                )
            if reported > 10000:
                raise SonarError(
                    "A análise excede o limite de 10.000 issues da API; reduza o escopo do corpus."
                )
            if total is not None and reported != total:
                raise SonarError(
                    "O total de issues mudou durante a paginação; snapshot inconsistente."
                )
            total = reported
            current = payload.get("issues", [])
            collected.extend(current)
            components.update({item["key"]: item for item in payload.get("components", [])})
            (artifacts / f"issues-page-{page}.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            if len(collected) >= total:
                break
            if not current:
                raise SonarError("A API interrompeu a paginação antes do total de issues.")
            page += 1
        if len(collected) != total or len({item.get("key") for item in collected}) != total:
            raise SonarError("A API retornou issues duplicadas ou quantidade divergente.")
        rules: dict[str, str] = {}
        for key in sorted({item["rule"] for item in collected}):
            payload = self._get("api/rules/show", key=key)
            rule = payload.get("rule", {})
            description = (
                rule.get("mdDesc")
                or rule.get("htmlDesc")
                or "\n".join(
                    section.get("content", "") for section in rule.get("descriptionSections", [])
                )
            )
            rules[key] = html.unescape(re.sub(r"<[^>]+>", " ", description)).strip()
        (artifacts / "rule-descriptions.json").write_text(
            json.dumps(rules, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        result = []
        for item in collected:
            if item.get("project") and item["project"] != self.project_key:
                raise SonarError("A API retornou um issue de outro projeto.")
            component_key = item["component"]
            relative = components.get(component_key, {}).get("path")
            if not relative and component_key.startswith(self.project_key + ":"):
                relative = component_key[len(self.project_key) + 1 :]
            if not relative:
                raise SonarError(f"Não foi possível resolver o arquivo do issue {item['key']}.")
            relative = relative.replace("\\", "/")
            _source_path(repo, relative)
            line = int(item.get("line") or item.get("textRange", {}).get("startLine") or 1)
            impact_severity = next(
                (
                    impact.get("severity")
                    for impact in item.get("impacts", [])
                    if impact.get("softwareQuality") == "MAINTAINABILITY"
                ),
                None,
            )
            severity = (
                impact_severity if self.config.mode == "mqr" else item.get("severity")
            ) or "MAJOR"
            result.append(
                Issue(
                    key=item["key"],
                    rule=item["rule"],
                    path=relative,
                    line=line,
                    message=item.get("message", ""),
                    severity=severity,
                    effort_minutes=_effort_minutes(item.get("effort", item.get("debt", 0))),
                    anchor=_anchor(repo, relative, line, item.get("message", "")),
                    rule_description=rules[item["rule"]],
                )
            )
        return sorted(result, key=lambda issue: (issue.path, issue.line, issue.rule, issue.key))

    def _metadata(self, artifacts: Path) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "simulated": False,
            "project_key": self.project_key,
            "mode": self.config.mode,
        }
        try:
            response = self.client.get(
                "api/server/version", timeout=self._timeout(min(self.config.timeout_seconds, 60))
            )
            response.raise_for_status()
            metadata["server_version"] = response.text.strip().strip('"')
        except httpx.HTTPError as exc:
            raise SonarError(
                self._redact(f"Falha ao consultar a versão do servidor: {exc}")
            ) from exc
        # Plugin listing can require administration; record unavailable metadata explicitly.
        for name, endpoint, params in [
            ("quality_profiles", "api/qualityprofiles/search", {"project": self.project_key}),
            ("plugins", "api/plugins/installed", {}),
        ]:
            try:
                metadata[name] = self._get(endpoint, **params)
            except SonarDeadlineExceeded:
                raise
            except SonarError as exc:
                metadata[name] = {"unavailable": str(exc)}
        (artifacts / "analysis-metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return metadata

    def analyze(self, repo: Path, artifacts: Path) -> Snapshot:
        repo, artifacts = Path(repo).resolve(), Path(artifacts).resolve()
        report = self._scanner(repo, artifacts)
        task = self._wait_for_analysis(report["ceTaskId"])
        analysis_id = task["analysisId"]
        self._assert_latest(analysis_id)
        issues = self._issues(repo, artifacts)
        metric_names = [
            "ncloc",
            "complexity",
            "cognitive_complexity",
            "duplicated_lines_density",
            "coverage",
        ]
        metric_names += (
            ["code_smells", "sqale_index"]
            if self.config.mode == "standard"
            else [
                "software_quality_maintainability_issues",
                "software_quality_maintainability_remediation_effort",
            ]
        )
        measures = self._get(
            "api/measures/component", component=self.project_key, metricKeys=",".join(metric_names)
        )
        component = measures.get("component", {})
        if component.get("key") != self.project_key:
            raise SonarError("As métricas não pertencem ao projeto solicitado.")
        metrics = {
            item["metric"]: float(item["value"])
            for item in component.get("measures", [])
            if "value" in item
        }
        metadata = self._metadata(artifacts)
        metadata["ce_task"] = task
        metadata["effort_day_minutes"] = 480
        self._assert_latest(analysis_id)
        return _save_snapshot(
            Snapshot(issues=issues, metrics=metrics, analysis_id=analysis_id, metadata=metadata),
            artifacts,
        )


class MockAnalyzer:
    """Marker-only fixture. This does not perform static analysis or produce research data."""

    def __init__(self, config: SonarConfig, project: ProjectConfig, project_key: str):
        self.config, self.project, self.project_key = config, project, project_key
        self.deadline: float | None = None

    def close(self) -> None:
        pass

    def analyze(self, repo: Path, artifacts: Path) -> Snapshot:
        repo = Path(repo).resolve()
        timeout = 30 if self.deadline is None else min(30, self.deadline - time.monotonic())
        if timeout <= 0:
            raise SonarDeadlineExceeded(
                "Limite de tempo do experimento atingido durante a análise."
            )
        try:
            tracked = (
                subprocess.run(
                    ["git", "ls-files", "-z"],
                    cwd=repo,
                    capture_output=True,
                    check=True,
                    timeout=timeout,
                    env=clean_environment(),
                )
                .stdout.decode("utf-8")
                .split("\0")
            )
        except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
            raise SonarError(
                f"Não foi possível listar arquivos Git da demonstração: {exc}"
            ) from exc
        issues = []
        for relative in sorted(filter(None, tracked)):
            if self.deadline is not None and time.monotonic() >= self.deadline:
                raise SonarDeadlineExceeded(
                    "Limite de tempo do experimento atingido durante a análise."
                )
            if not any(
                fnmatch.fnmatchcase(relative, pattern)
                or (pattern.startswith("**/") and fnmatch.fnmatchcase(relative, pattern[3:]))
                for pattern in self.project.source_globs
            ):
                continue
            path = _source_path(repo, relative)
            if not path.is_file():
                continue
            for line, source in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                marker = next(
                    (
                        marker
                        for marker in ("DEMO_UNUSED", "DEMO_DYNAMIC")
                        if "# " + marker in source
                    ),
                    None,
                )
                if marker:
                    key = hashlib.sha256(
                        f"{relative}|mock:unused-local|{marker}".encode()
                    ).hexdigest()[:24]
                    issues.append(
                        Issue(
                            key=key,
                            rule="mock:unused-local",
                            path=relative,
                            line=line,
                            message="Remove this unused local variable (simulated marker).",
                            effort_minutes=5,
                            anchor=source.strip(),
                            rule_description="Simulated unused local variable rule. Inspect dynamic accesses before removing assignments.",
                        )
                    )
        snapshot = Snapshot(
            issues=issues,
            metrics={"code_smells": float(len(issues)), "sqale_index": float(5 * len(issues))},
            analysis_id="mock-" + uuid.uuid4().hex,
            metadata={
                "simulated": True,
                "project_key": self.project_key,
                "scanner": "marker-fixture-v1",
            },
        )
        return _save_snapshot(snapshot, Path(artifacts))


def make_analyzer(
    config: SonarConfig, project: ProjectConfig, project_key: str
) -> SonarAnalyzer | MockAnalyzer:
    return (MockAnalyzer if config.backend == "mock" else SonarAnalyzer)(
        config, project, project_key
    )
