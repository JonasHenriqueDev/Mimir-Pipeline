from pathlib import Path

import pytest
from pydantic import ValidationError

from tcc_pipeline.config import LLMConfig, PipelineConfig, ProjectConfig, SonarConfig, load_config


def project():
    return ProjectConfig(name="sample", repo=".", test_commands=[["python", "-m", "unittest"]])


def test_relative_paths_are_resolved_against_config_file(tmp_path):
    folder = tmp_path / "configs"
    folder.mkdir()
    path = folder / "config.yaml"
    path.write_text(
        "project:\n  name: example\n  repo: ../repository\n  test_commands: [[python, -m, unittest]]\noutput_dir: ../output\n",
        encoding="utf-8",
    )
    config = load_config(path)
    assert Path(config.project.repo) == tmp_path / "repository"
    assert Path(config.output_dir) == tmp_path / "output"


def test_config_rejects_unknown_options():
    with pytest.raises(ValidationError, match="Extra inputs"):
        PipelineConfig(project=project(), unknown=True)


def test_simulated_and_real_backends_cannot_mix():
    with pytest.raises(ValidationError, match="não misture"):
        PipelineConfig(project=project(), sonar=SonarConfig(backend="mock"))


def test_cost_budget_requires_both_prices():
    with pytest.raises(ValidationError, match="preços"):
        PipelineConfig(project=project(), llm=LLMConfig(max_cost_usd=1))


@pytest.mark.parametrize("name", ["python", "java", "typescript"])
def test_real_examples_load(name):
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "configs" / f"{name}.example.yaml")
    assert config.project.language == name
    assert "@sha256:" in config.runner.image
    assert config.runner.network == "none"
