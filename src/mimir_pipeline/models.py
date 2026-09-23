"""Shared, versioned data contracts. Never store credentials in these objects."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Issue(Model):
    key: str
    rule: str
    path: str
    line: int = 1
    message: str
    severity: str = "MAJOR"
    effort_minutes: float = 0
    anchor: str = ""
    rule_description: str = ""
    tracking_id: str = ""

    @property
    def identity(self) -> str:
        return self.tracking_id or f"{self.rule}|{self.path}|{self.anchor or self.message}"


class Snapshot(Model):
    issues: list[Issue]
    metrics: dict[str, float] = Field(default_factory=dict)
    analysis_id: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class Classification(Model):
    label: Literal["pertinent", "false_positive", "inconclusive"]
    rationale: str
    evidence: list[str]


class Edit(Model):
    path: str
    old_text: str
    new_text: str


class PatchProposal(Model):
    edits: list[Edit]
    explanation: str
    expected_effect: str
    risks: list[str]


class CommandResult(Model):
    command: list[str]
    returncode: int
    duration_seconds: float
    timed_out: bool = False
    log_path: str


class Attempt(Model):
    index: int
    iteration: int
    issue: Issue
    status: str
    reason: str = ""
    classification: Classification | None = None
    proposal: PatchProposal | None = None
    commands: list[CommandResult] = Field(default_factory=list)
    commit: str | None = None
    artifact_dir: str = ""


class RunResult(Model):
    schema_version: int = 1
    run_id: str
    experiment_id: str
    project: str
    condition: Literal["filtered", "unfiltered"]
    repetition: int = 1
    base_commit: str
    status: str = "running"
    stop_reason: str = ""
    started_at: str
    ended_at: str = ""
    baseline: Snapshot | None = None
    final: Snapshot | None = None
    attempts: list[Attempt] = Field(default_factory=list)
    llm_usage: dict[str, Any] = Field(default_factory=dict)
    worktree: str = ""
    simulated: bool = False
    error: str | None = None
