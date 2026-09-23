"""Self-contained fixture: static markers simulate two issues, real tests verify behavior."""

import uuid
from pathlib import Path

from .config import (
    ExperimentConfig,
    LLMConfig,
    PipelineConfig,
    ProjectConfig,
    RunnerConfig,
    SonarConfig,
)
from .workspace import git

SOURCE = '''"""Fixture didática; os marcadores pertencem apenas ao scanner simulado."""


def total(price, quantity):
    unused = 123  # DEMO_UNUSED
    return price * quantity


def dynamic_value():
    dynamic = 7  # DEMO_DYNAMIC
    return locals()["dynamic"]
'''

TESTS = """import unittest

from app import dynamic_value, total


class BehaviorTests(unittest.TestCase):
    def test_total(self):
        self.assertEqual(total(3, 4), 12)

    def test_zero_quantity(self):
        self.assertEqual(total(3, 0), 0)

    def test_dynamic_lookup(self):
        self.assertEqual(dynamic_value(), 7)


if __name__ == "__main__":
    unittest.main()
"""


def create_demo(output: Path) -> PipelineConfig:
    output = output.resolve()
    repo = output / "demo-sources" / uuid.uuid4().hex[:10]
    (repo / "tests").mkdir(parents=True)
    (repo / "app.py").write_text(SOURCE, encoding="utf-8", newline="\n")
    (repo / "tests" / "test_app.py").write_text(TESTS, encoding="utf-8", newline="\n")
    (repo / ".gitignore").write_text("__pycache__/\n.scannerwork/\n", encoding="utf-8")
    git(repo, "init", "-b", "demo")
    git(repo, "config", "core.autocrlf", "false")
    git(repo, "add", "app.py", "tests/test_app.py", ".gitignore")
    git(
        repo,
        "-c",
        "user.name=TCC Demo",
        "-c",
        "user.email=demo@localhost",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-m",
        "Fixture demonstrativa",
    )
    return PipelineConfig(
        project=ProjectConfig(
            name="demo-python",
            repo=str(repo),
            language="python",
            source_globs=["app.py"],
            allowed_edit_globs=["app.py"],
            test_commands=[["{python}", "-m", "unittest", "discover", "-s", "tests", "-v"]],
        ),
        runner=RunnerConfig(mode="local", timeout_seconds=30),
        sonar=SonarConfig(backend="mock"),
        llm=LLMConfig(provider="mock", max_calls=20),
        experiment=ExperimentConfig(max_iterations=3, max_attempts_per_issue=1),
        output_dir=str(output),
    )
