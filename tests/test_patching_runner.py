import sys
from pathlib import Path

import pytest

from mimir_pipeline.config import RunnerConfig
from mimir_pipeline.demo import create_demo
from mimir_pipeline.models import Edit, PatchProposal
from mimir_pipeline.patching import PatchError, apply_proposal, safe_path
from mimir_pipeline.runner import Runner, clean_environment
from mimir_pipeline.workspace import git, tracked_changes


def proposal(*edits):
    return PatchProposal(
        edits=list(edits), explanation="fix", expected_effect="maintainability", risks=[]
    )


@pytest.mark.parametrize(
    "path", ["../outside.py", "/outside.py", "C:/outside.py", "dir\\file.py", ".git/config"]
)
def test_path_traversal_rejected(tmp_path, path):
    with pytest.raises(PatchError):
        safe_path(tmp_path, path)


def test_patch_validation_is_atomic(tmp_path):
    config = create_demo(tmp_path)
    repo = Path(config.project.repo)
    before = (repo / "app.py").read_bytes()
    edits = proposal(
        Edit(path="app.py", old_text="    unused = 123  # DEMO_UNUSED\n", new_text=""),
        Edit(path="tests/test_app.py", old_text="assertEqual", new_text="something"),
    )
    with pytest.raises(PatchError, match="protegido"):
        apply_proposal(repo, edits, config.project, config.experiment)
    assert (repo / "app.py").read_bytes() == before


def test_ambiguous_edit_is_rejected(tmp_path):
    config = create_demo(tmp_path)
    with pytest.raises(PatchError, match="exatamente uma vez"):
        apply_proposal(
            Path(config.project.repo),
            proposal(Edit(path="app.py", old_text="def ", new_text="async def ")),
            config.project,
            config.experiment,
        )


def test_suppression_cannot_masquerade_as_a_fix(tmp_path):
    config = create_demo(tmp_path)
    with pytest.raises(PatchError, match="Supressão"):
        apply_proposal(
            Path(config.project.repo),
            proposal(
                Edit(path="app.py", old_text="unused = 123", new_text="unused = 123  # NOSONAR")
            ),
            config.project,
            config.experiment,
        )


@pytest.mark.parametrize("filename", ["cálculo.py", " arquivo com espaços.py"])
def test_unicode_and_whitespace_paths_are_preserved_during_patch_validation(tmp_path, filename):
    config = create_demo(tmp_path)
    config.project.allowed_edit_globs = ["*.py"]
    repo = Path(config.project.repo)
    (repo / filename).write_bytes(b"unused = 123\nvalue = 1\n")
    git(repo, "add", "--", filename)
    git(repo, "commit", "-m", "Add source with non-ASCII or whitespace path")
    # Force Git's normal quoted-path mode; the pipeline must still obtain exact names.
    git(repo, "config", "core.quotepath", "true")

    diff, paths = apply_proposal(
        repo,
        proposal(Edit(path=filename, old_text="unused = 123\n", new_text="")),
        config.project,
        config.experiment,
    )

    assert paths == [filename]
    assert tracked_changes(repo) == {filename}
    assert filename in diff
    assert (repo / filename).read_text(encoding="utf-8") == "value = 1\n"


def test_credentials_not_exposed_to_project_commands(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    monkeypatch.setenv("SONAR_TOKEN", "secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    env = clean_environment()
    assert "OPENAI_API_KEY" not in env
    assert "SONAR_TOKEN" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert "PATH" in env or "Path" in env


def test_process_timeout_is_observed_and_logged(tmp_path):
    with Runner(
        RunnerConfig(mode="local", timeout_seconds=1), tmp_path, tmp_path / "logs"
    ) as runner:
        result = runner.run(
            [sys.executable, "-c", "import time; print('start', flush=True); time.sleep(20)"],
            "timeout",
        )
    assert result.timed_out
    assert result.returncode != 0
    assert "start" in Path(result.log_path).read_text(encoding="utf-8")


def test_command_arguments_are_not_executed_as_shell(tmp_path):
    payload = "literal & echo wrong"
    with Runner(RunnerConfig(mode="local"), tmp_path, tmp_path / "logs") as runner:
        result = runner.run(["{python}", "-c", "import sys; print(sys.argv[1])", payload], "args")
    assert result.returncode == 0
    assert Path(result.log_path).read_text(encoding="utf-8").strip() == payload
