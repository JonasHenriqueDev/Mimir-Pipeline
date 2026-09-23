import json
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from tcc_pipeline.config import ProjectConfig, SonarConfig, load_config
from tcc_pipeline.sonar import (
    MockAnalyzer,
    SonarAnalyzer,
    SonarDeadlineExceeded,
    SonarError,
    _effort_minutes,
)


@pytest.fixture
def project(tmp_path):
    return ProjectConfig(
        name="unit", repo=str(tmp_path), test_commands=[["python", "-m", "unittest"]]
    )


@pytest.fixture
def analyzer(project, monkeypatch):
    monkeypatch.setenv("SONAR_TOKEN", "secret-for-unit-test")
    result = SonarAnalyzer(SonarConfig(), project, "unit-key")
    yield result
    result.close()


def transport(analyzer, handler):
    analyzer.client.close()
    analyzer.client = httpx.Client(
        base_url="http://localhost:9000/",
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer secret-for-unit-test"},
    )


def issue(key="issue-1", path="a.py", line=1):
    return {
        "key": key,
        "project": "unit-key",
        "component": "unit-key:" + path,
        "rule": "python:S1481",
        "line": line,
        "message": "Remove unused variable.",
        "effort": "1h 5min",
    }


def test_complete_analysis_paginates_and_preserves_all_rules(analyzer, tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("    unused = 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("other = 2\n", encoding="utf-8")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    analyzer.config.rules = ["some-other-rule"]
    monkeypatch.setattr(analyzer, "_scanner", lambda repo, output: {"ceTaskId": "task-1"})
    calls = []

    def handler(request):
        calls.append(request)
        assert request.headers["Authorization"] == "Bearer secret-for-unit-test"
        path = request.url.path
        if path == "/api/ce/task":
            return httpx.Response(
                200,
                json={
                    "task": {
                        "id": "task-1",
                        "componentKey": "unit-key",
                        "status": "SUCCESS",
                        "analysisId": "analysis-1",
                    }
                },
            )
        if path == "/api/project_analyses/search":
            return httpx.Response(200, json={"analyses": [{"key": "analysis-1"}]})
        if path == "/api/issues/search":
            assert request.url.params["types"] == "CODE_SMELL"
            assert request.url.params["components"] == "unit-key"
            assert "rules" not in request.url.params
            page = request.url.params["p"]
            return httpx.Response(
                200,
                json={
                    "paging": {"total": 2},
                    "issues": [issue()] if page == "1" else [issue("issue-2", "b.py")],
                },
            )
        if path == "/api/rules/show":
            return httpx.Response(200, json={"rule": {"htmlDesc": "<p>Remove &amp; explain.</p>"}})
        if path == "/api/measures/component":
            return httpx.Response(
                200,
                json={
                    "component": {
                        "key": "unit-key",
                        "measures": [{"metric": "sqale_index", "value": "130"}],
                    }
                },
            )
        if path == "/api/server/version":
            return httpx.Response(200, text="26.9.0.129388")
        if path == "/api/qualityprofiles/search":
            return httpx.Response(200, json={"profiles": [{"key": "profile-1"}]})
        if path == "/api/plugins/installed":
            return httpx.Response(403, json={"errors": [{"msg": "Requires admin"}]})
        raise AssertionError(path)

    transport(analyzer, handler)
    result = analyzer.analyze(tmp_path, artifacts)
    assert len(result.issues) == 2
    assert result.issues[0].anchor == "unused = 1"
    assert result.issues[0].effort_minutes == 65
    assert result.issues[0].rule_description == "Remove & explain."
    assert result.analysis_id == "analysis-1"
    assert result.metrics["sqale_index"] == 130
    assert result.metadata["simulated"] is False
    assert "unavailable" in result.metadata["plugins"]
    assert len([req for req in calls if req.url.path == "/api/project_analyses/search"]) == 2
    assert json.loads((artifacts / "snapshot.json").read_text())["analysis_id"] == "analysis-1"


def test_mqr_uses_maintainability_filter(analyzer, tmp_path):
    analyzer.config.mode = "mqr"

    def handler(request):
        assert request.url.params["impactSoftwareQualities"] == "MAINTAINABILITY"
        assert "types" not in request.url.params
        return httpx.Response(200, json={"paging": {"total": 0}, "issues": []})

    transport(analyzer, handler)
    assert analyzer._issues(tmp_path, tmp_path) == []


def test_mqr_uses_maintainability_impact_severity(analyzer, tmp_path):
    analyzer.config.mode = "mqr"
    row = issue()
    row.update(
        severity="BLOCKER", impacts=[{"softwareQuality": "MAINTAINABILITY", "severity": "LOW"}]
    )

    def handler(request):
        if request.url.path == "/api/rules/show":
            return httpx.Response(200, json={"rule": {"htmlDesc": "description"}})
        return httpx.Response(200, json={"paging": {"total": 1}, "issues": [row]})

    transport(analyzer, handler)
    assert analyzer._issues(tmp_path, tmp_path)[0].severity == "LOW"


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"paging": {"total": 10001}, "issues": []}, "10.000"),
        ({"paging": {"total": 2}, "issues": [issue(), issue()]}, "duplicadas"),
        ({"paging": {"total": 1}, "issues": []}, "paginação"),
        ({"issues": []}, "completude"),
    ],
)
def test_incomplete_issue_sets_are_rejected(analyzer, tmp_path, payload, expected):
    transport(analyzer, lambda request: httpx.Response(200, json=payload))
    with pytest.raises(SonarError, match=expected):
        analyzer._issues(tmp_path, tmp_path)


def test_pagination_rejects_changing_total(analyzer, tmp_path):
    def handler(request):
        return httpx.Response(
            200,
            json={
                "paging": {"total": 2 if request.url.params["p"] == "1" else 3},
                "issues": [issue()],
            },
        )

    transport(analyzer, handler)
    with pytest.raises(SonarError, match="mudou"):
        analyzer._issues(tmp_path, tmp_path)


@pytest.mark.parametrize(
    "task,expected",
    [
        (
            {"id": "task-1", "componentKey": "different", "status": "SUCCESS", "analysisId": "a"},
            "não corresponde",
        ),
        (
            {
                "id": "task-1",
                "componentKey": "unit-key",
                "status": "FAILED",
                "errorMessage": "scanner bad",
            },
            "FAILED",
        ),
        ({"id": "task-1", "componentKey": "unit-key", "status": "SUCCESS"}, "analysisId"),
    ],
)
def test_ce_task_must_complete_for_expected_project(analyzer, task, expected):
    transport(analyzer, lambda request: httpx.Response(200, json={"task": task}))
    with pytest.raises(SonarError, match=expected):
        analyzer._wait_for_analysis("task-1")


def test_latest_analysis_check_rejects_concurrent_scan(analyzer):
    transport(analyzer, lambda request: httpx.Response(200, json={"analyses": [{"key": "newer"}]}))
    with pytest.raises(SonarError, match="substituída"):
        analyzer._assert_latest("expected")


def test_scanner_removes_stale_report(analyzer, tmp_path):
    report_dir = tmp_path / ".scannerwork"
    report_dir.mkdir()
    (report_dir / "report-task.txt").write_text("projectKey=unit-key\nceTaskId=old\n")
    analyzer.config.scanner_command = [
        sys.executable,
        "-c",
        "print('scan returned without metadata')",
    ]
    with pytest.raises(SonarError, match="não gerou"):
        analyzer._scanner(tmp_path, tmp_path / "artifacts")
    assert not (report_dir / "report-task.txt").exists()


def test_scanner_token_is_env_only_and_redacted(analyzer, tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "llm-key-not-for-scanner")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "cloud-key-not-for-scanner")
    analyzer.config.scanner_url = "http://sonarqube:9000"
    script = tmp_path / "fake_scanner.py"
    script.write_text(
        "import os, pathlib\n"
        "print(os.environ['SONAR_TOKEN'])\n"
        "assert 'OPENAI_API_KEY' not in os.environ\n"
        "assert 'AWS_SECRET_ACCESS_KEY' not in os.environ\n"
        "assert os.environ['SONAR_HOST_URL'] == 'http://sonarqube:9000'\n"
        "pathlib.Path('.scannerwork').mkdir(exist_ok=True)\n"
        "pathlib.Path('.scannerwork/report-task.txt').write_text('projectKey=unit-key\\nceTaskId=new-task\\n')\n",
        encoding="utf-8",
    )
    analyzer.config.scanner_command = [sys.executable, str(script)]
    artifacts = tmp_path / "artifacts"
    report = analyzer._scanner(tmp_path, artifacts)
    assert report["ceTaskId"] == "new-task"
    assert "secret-for-unit-test" not in (artifacts / "scanner-command.json").read_text()
    assert "secret-for-unit-test" not in (artifacts / "scanner.log").read_text()
    assert "[REDACTED]" in (artifacts / "scanner.log").read_text()
    assert "-Dsonar.host.url=http://sonarqube:9000" in json.loads(
        (artifacts / "scanner-command.json").read_text()
    )
    with pytest.raises(SonarError, match="reutilizou"):
        analyzer._scanner(tmp_path, artifacts)


def test_scanner_timeout_fails_and_writes_log(analyzer, tmp_path):
    analyzer.config.scanner_command = [
        sys.executable,
        "-c",
        "import time; print('started', flush=True); time.sleep(60)",
    ]
    analyzer.config.timeout_seconds = 1
    with pytest.raises(SonarError, match="limite de tempo"):
        analyzer._scanner(tmp_path, tmp_path / "artifacts")
    assert "started" in (tmp_path / "artifacts" / "scanner.log").read_text()


def test_token_required(project, monkeypatch):
    monkeypatch.delenv("SONAR_TOKEN", raising=False)
    with pytest.raises(SonarError, match="SONAR_TOKEN"):
        SonarAnalyzer(SonarConfig(), project, "key")


def test_source_escape_rejected(analyzer, tmp_path):
    def handler(request):
        if request.url.path == "/api/rules/show":
            return httpx.Response(200, json={"rule": {"htmlDesc": "description"}})
        return httpx.Response(
            200, json={"paging": {"total": 1}, "issues": [issue(path="../secret.py")]}
        )

    transport(analyzer, handler)
    with pytest.raises(SonarError, match="Caminho inválido"):
        analyzer._issues(tmp_path, tmp_path)


def test_mock_only_tracked_matching_files_and_stable_keys(project, tmp_path):
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    source = tmp_path / "demo.py"
    source.write_text(
        "unused = 123  # DEMO_UNUSED\ndynamic = 7  # DEMO_DYNAMIC\n", encoding="utf-8"
    )
    (tmp_path / "not_source.txt").write_text("# DEMO_UNUSED\n")
    (tmp_path / "untracked.py").write_text("# DEMO_UNUSED\n")
    subprocess.run(["git", "add", "demo.py", "not_source.txt"], cwd=tmp_path, check=True)
    analyzer = MockAnalyzer(SonarConfig(backend="mock"), project, "mock-project")
    first = analyzer.analyze(tmp_path, tmp_path / "first")
    source.write_text("\n" + source.read_text(), encoding="utf-8")
    second = analyzer.analyze(tmp_path, tmp_path / "second")
    assert len(first.issues) == 2
    assert {item.key for item in first.issues} == {item.key for item in second.issues}
    assert {item.identity for item in first.issues} == {item.identity for item in second.issues}
    assert first.metadata["simulated"] is True
    assert first.metrics == {"code_smells": 2, "sqale_index": 10}
    assert first.analysis_id != second.analysis_id


def test_effort_uses_sonar_eight_hour_workday():
    assert _effort_minutes("1d 2h 30min") == 630
    assert _effort_minutes("10min") == 10
    assert _effort_minutes(5) == 5
    assert _effort_minutes(None) == 0


def test_http_timeout_is_limited_by_experiment_deadline(analyzer):
    analyzer.deadline = time.monotonic() + 0.5

    def handler(request):
        assert 0 < request.extensions["timeout"]["read"] <= 0.5
        return httpx.Response(200, json={})

    transport(analyzer, handler)
    analyzer._get("api/ce/task")


def test_ce_polling_honors_experiment_deadline(analyzer, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("tcc_pipeline.sonar.time.monotonic", lambda: clock[0])
    monkeypatch.setattr(
        "tcc_pipeline.sonar.time.sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )
    analyzer.deadline = 100.5
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200, json={"task": {"id": "task-1", "componentKey": "unit-key", "status": "PENDING"}}
        )

    transport(analyzer, handler)
    with pytest.raises(SonarDeadlineExceeded):
        analyzer._wait_for_analysis("task-1")
    assert len(calls) == 1
    assert clock[0] == 100.5


def test_scanner_honors_experiment_deadline(analyzer, tmp_path):
    analyzer.config.scanner_command = [sys.executable, "-c", "import time; time.sleep(60)"]
    analyzer.deadline = time.monotonic() + 0.1
    with pytest.raises(SonarDeadlineExceeded):
        analyzer._scanner(tmp_path, tmp_path / "artifacts")


def test_expired_deadline_prevents_starting_scanner(analyzer, tmp_path, monkeypatch):
    analyzer.deadline = time.monotonic() - 1
    monkeypatch.setattr(
        "tcc_pipeline.sonar.subprocess.Popen",
        lambda *args, **kwargs: pytest.fail("scanner should not start"),
    )
    with pytest.raises(SonarDeadlineExceeded):
        analyzer._scanner(tmp_path, tmp_path / "artifacts")


def test_expired_deadline_prevents_mock_analysis(project, tmp_path):
    analyzer = MockAnalyzer(SonarConfig(backend="mock"), project, "mock-project")
    analyzer.deadline = time.monotonic() - 1
    with pytest.raises(SonarDeadlineExceeded):
        analyzer.analyze(tmp_path, tmp_path / "artifacts")


@pytest.mark.parametrize("language", ["python", "java", "typescript"])
def test_example_configs_validate(language):
    config = load_config(
        Path(__file__).resolve().parents[1] / "configs" / f"{language}.example.yaml"
    )
    assert config.sonar.scanner_url == "http://sonarqube:9000"
    assert config.sonar.properties["sonar.scm.disabled"] == "true"
    assert "@sha256:" in config.runner.image


def test_java_scanner_can_see_compiled_classes_and_dependency_libraries():
    config = load_config(Path(__file__).resolve().parents[1] / "configs" / "java.example.yaml")
    assert config.sonar.properties["sonar.java.binaries"] == "target/classes"
    assert config.sonar.properties["sonar.java.test.binaries"] == "target/test-classes"
    for scope, folder, prop in [
        ("compile", "sonar-libraries", "sonar.java.libraries"),
        ("test", "sonar-test-libraries", "sonar.java.test.libraries"),
    ]:
        assert any(
            f"-DincludeScope={scope}" in command and f"-DoutputDirectory=target/{folder}" in command
            for command in config.project.setup_commands
        )
        assert config.sonar.properties[prop] == f"target/{folder}/**/*.jar"
