"""Validated configuration; all relative paths resolve against the YAML file."""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field, model_validator

from .models import Model


class ProjectConfig(Model):
    name: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    repo: str
    ref: str = "HEAD"
    language: Literal["python", "java", "typescript"] = "python"
    source_globs: list[str] = Field(default_factory=lambda: ["**/*.py"])
    context_files: list[str] = Field(default_factory=list)
    allowed_edit_globs: list[str] = Field(default_factory=lambda: ["**/*.py", "*.py"])
    protected_globs: list[str] = Field(
        default_factory=lambda: [
            "tests/**",
            "test/**",
            "**/test/**",
            "**/tests/**",
            "**/test_*.py",
            "**/*Test.java",
            "**/*.test.*",
            "**/*.spec.*",
            ".git/**",
            ".github/**",
            "**/.env*",
            "**/package*.json",
            "**/*lock*",
            "**/pom.xml",
            "**/build.gradle*",
            "**/pyproject.toml",
            "**/sonar-project.properties",
        ]
    )
    setup_commands: list[list[str]] = Field(default_factory=list)
    build_commands: list[list[str]] = Field(default_factory=list)
    test_commands: list[list[str]] = Field(min_length=1)


class RunnerConfig(Model):
    mode: Literal["local", "docker"] = "docker"
    image: str = "python:3.12-slim"
    timeout_seconds: int = Field(default=300, ge=1)
    network: str = "none"
    setup_network: str = "bridge"
    memory: str = "2g"
    cpus: float = Field(default=2, gt=0)


class SonarConfig(Model):
    backend: Literal["sonar", "mock"] = "sonar"
    url: str = "http://localhost:9000"
    scanner_url: str | None = None
    token_env: str = "SONAR_TOKEN"
    scanner_command: list[str] = Field(default_factory=lambda: ["sonar-scanner"])
    properties: dict[str, str] = Field(default_factory=dict)
    rules: list[str] = Field(default_factory=list)
    timeout_seconds: int = Field(default=600, ge=1)
    poll_seconds: float = Field(default=2, gt=0)
    mode: Literal["standard", "mqr"] = "standard"


class LLMConfig(Model):
    provider: Literal["openai", "openai_compatible", "mock"] = "openai"
    base_url: str = "https://api.openai.com/v1"
    api_key_env: str = "OPENAI_API_KEY"
    model: str = ""
    temperature: float | None = None
    max_output_tokens: int = Field(default=4096, ge=128)
    timeout_seconds: int = Field(default=120, ge=1)
    retries: int = Field(default=2, ge=0, le=5)
    max_calls: int = Field(default=30, ge=1)
    max_cost_usd: float | None = Field(default=None, gt=0)
    input_price_per_million: float | None = Field(default=None, ge=0)
    output_price_per_million: float | None = Field(default=None, ge=0)
    max_context_chars: int = Field(default=40000, ge=1000)


class ExperimentConfig(Model):
    max_iterations: int = Field(default=3, ge=1)
    max_attempts: int = Field(default=20, ge=1)
    max_attempts_per_issue: int = Field(default=2, ge=1)
    max_seconds: int = Field(default=3600, ge=1)
    max_patch_chars: int = Field(default=20000, ge=1)
    max_files_per_patch: int = Field(default=3, ge=1)
    repetitions: int = Field(default=1, ge=1)
    seed: int = 42
    reject_new_issues: bool = True


class PipelineConfig(Model):
    project: ProjectConfig
    runner: RunnerConfig = Field(default_factory=RunnerConfig)
    sonar: SonarConfig = Field(default_factory=SonarConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    experiment: ExperimentConfig = Field(default_factory=ExperimentConfig)
    output_dir: str = "../artifacts"

    @model_validator(mode="after")
    def validate_backends(self):
        if (self.sonar.backend == "mock") != (self.llm.provider == "mock"):
            raise ValueError(
                "Use os dois backends mock juntos; não misture resultados simulados e reais."
            )
        if self.llm.max_cost_usd is not None and (
            self.llm.input_price_per_million is None or self.llm.output_price_per_million is None
        ):
            raise ValueError("O limite de custo exige os preços de entrada e saída por milhão.")
        return self


def load_config(path: Path) -> PipelineConfig:
    path = path.resolve()
    config = PipelineConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    for obj, attr in [(config.project, "repo"), (config, "output_dir")]:
        value = Path(getattr(obj, attr))
        if not value.is_absolute():
            setattr(obj, attr, str((path.parent / value).resolve()))
    return config
