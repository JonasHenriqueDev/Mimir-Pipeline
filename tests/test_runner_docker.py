"""Docker command contracts; no daemon is needed and no containers are launched."""

import subprocess
from types import SimpleNamespace

import pytest

from mimir_pipeline.config import RunnerConfig
from mimir_pipeline.models import CommandResult
from mimir_pipeline.runner import Runner, RunnerError, bounded_process


@pytest.fixture
def docker_runner(tmp_path, monkeypatch):
    common = tmp_path / ".git"
    git_dir = common / "worktrees" / "unit"

    def fake_git(repo, *args):
        assert repo == tmp_path
        if args == ("rev-parse", "--git-common-dir"):
            return ".git"  # Exercise a relative common directory on Windows as well.
        if args == ("rev-parse", "--absolute-git-dir"):
            return str(git_dir)
        pytest.fail(f"Unexpected Git call: {args}")

    monkeypatch.setattr("mimir_pipeline.runner.git", fake_git)
    result = Runner(RunnerConfig(mode="docker"), tmp_path, tmp_path / "logs")
    return result


def test_docker_creation_maps_worktree_git_as_readonly(docker_runner, monkeypatch):
    calls = []
    monkeypatch.setattr(docker_runner, "docker", lambda args: calls.append(args) or "container-id")
    docker_runner.__enter__()
    create, start = calls
    assert create[0] == "create"
    assert create[create.index("--network") + 1] == "bridge"
    assert create[create.index("--entrypoint") + 1] == "/bin/sh"
    assert create[create.index("--workdir") + 1] == "/workspace"
    assert "--cap-drop=ALL" in create
    assert "--security-opt=no-new-privileges" in create
    assert "GIT_DIR=/git/worktrees/unit" in create
    assert "GIT_WORK_TREE=/workspace" in create
    assert f"type=bind,source={docker_runner.repo / '.git'},target=/git,readonly" in create
    assert create[-3:] == [docker_runner.config.image, "-c", "while :; do sleep 3600; done"]
    assert start == ["start", docker_runner.container]


def test_finish_setup_disconnects_network_once_for_offline_tests(docker_runner, monkeypatch):
    calls = []
    docker_runner.container = "unit-container"
    monkeypatch.setattr(docker_runner, "docker", lambda args: calls.append(args) or "")
    docker_runner.finish_setup()
    docker_runner.finish_setup()
    assert calls == [["network", "disconnect", "bridge", "unit-container"]]
    assert docker_runner.ready


def test_finish_setup_moves_to_requested_test_network(docker_runner, monkeypatch):
    calls = []
    docker_runner.container = "unit-container"
    docker_runner.config.network = "test-isolated"
    monkeypatch.setattr(docker_runner, "docker", lambda args: calls.append(args) or "")
    docker_runner.finish_setup()
    assert calls == [
        ["network", "disconnect", "bridge", "unit-container"],
        ["network", "connect", "test-isolated", "unit-container"],
    ]


def test_docker_exec_timeout_removes_whole_container(docker_runner, monkeypatch):
    calls = []
    docker_runner.container = "unit-container"

    def fake_process(command, cwd, log, timeout):
        assert command == [
            "docker",
            "exec",
            "unit-container",
            "python",
            "-m",
            "pytest",
            "/workspace",
        ]
        return CommandResult(
            command=command, returncode=-1, timed_out=True, duration_seconds=0.1, log_path=str(log)
        )

    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("mimir_pipeline.runner.bounded_process", fake_process)
    monkeypatch.setattr("mimir_pipeline.runner.subprocess.run", fake_run)
    outcome = docker_runner.run(["{python}", "-m", "pytest", "{repo}"], "test")
    assert outcome.timed_out
    assert calls == [["docker", "rm", "-f", "unit-container"]]
    assert docker_runner.container is None
    assert outcome.command == ["python", "-m", "pytest", "/workspace"]


def test_docker_client_does_not_inherit_model_credentials(docker_runner, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "unit-secret")
    monkeypatch.setenv("SONAR_TOKEN", "unit-sonar-secret")

    def fake_run(command, **kwargs):
        assert "OPENAI_API_KEY" not in kwargs["env"]
        assert "SONAR_TOKEN" not in kwargs["env"]
        return SimpleNamespace(returncode=0, stdout="ready", stderr="")

    monkeypatch.setattr("mimir_pipeline.runner.subprocess.run", fake_run)
    assert docker_runner.docker(["version"]) == "ready"


@pytest.mark.parametrize(
    "cleanup_error", [OSError("Docker unavailable"), subprocess.TimeoutExpired("docker", 30)]
)
def test_failed_creation_preserves_error_when_cleanup_also_fails(
    docker_runner, monkeypatch, cleanup_error
):
    def create_fails(args):
        raise RunnerError("original create failure")

    def cleanup_fails(*args, **kwargs):
        raise cleanup_error

    monkeypatch.setattr(docker_runner, "docker", create_fails)
    monkeypatch.setattr("mimir_pipeline.runner.subprocess.run", cleanup_fails)
    with (
        pytest.warns(RuntimeWarning, match="remover"),
        pytest.raises(RunnerError, match="original create failure"),
    ):
        docker_runner.__enter__()
    assert docker_runner.container is None


def test_context_exit_preserves_build_error_when_cleanup_fails(docker_runner, monkeypatch):
    docker_runner.container = "unit-container"

    def cleanup_fails(*args, **kwargs):
        raise OSError("Docker unavailable during cleanup")

    monkeypatch.setattr("mimir_pipeline.runner.subprocess.run", cleanup_fails)
    # __exit__ must not replace the exception already raised inside the context.
    with pytest.warns(RuntimeWarning, match="remover"):
        assert not docker_runner.__exit__(RuntimeError, RuntimeError("build failed"), None)
    assert docker_runner.container is None


def test_interrupted_local_process_is_stopped(tmp_path, monkeypatch):
    stopped = []

    class InterruptedProcess:
        def wait(self, **kwargs):
            raise KeyboardInterrupt

    process = InterruptedProcess()
    monkeypatch.setattr("mimir_pipeline.runner.subprocess.Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr("mimir_pipeline.runner.stop_process", stopped.append)
    with pytest.raises(KeyboardInterrupt):
        bounded_process(["python", "test.py"], tmp_path, tmp_path / "test.log", 60)
    assert stopped == [process]
