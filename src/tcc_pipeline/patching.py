"""Validate the full edit set before changing any file."""

import difflib
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath

from .config import ExperimentConfig, ProjectConfig
from .models import PatchProposal
from .workspace import git


class PatchError(ValueError):
    pass


def matches(path: str, patterns: list[str]) -> bool:
    return any(
        fnmatchcase(path, pattern) or (pattern.startswith("**/") and fnmatchcase(path, pattern[3:]))
        for pattern in patterns
    )


def safe_path(repo: Path, relative: str) -> Path:
    posix = PurePosixPath(relative)
    if (
        not relative
        or "\\" in relative
        or ":" in relative
        or posix.is_absolute()
        or ".." in posix.parts
        or ".git" in posix.parts
    ):
        raise PatchError(f"Caminho inválido: {relative}")
    target = repo.joinpath(*posix.parts)
    for part in [target, *target.parents]:
        if part == repo:
            break
        if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
            raise PatchError(f"Link não permitido: {relative}")
    if not target.resolve().is_relative_to(repo.resolve()):
        raise PatchError(f"Caminho fora do repositório: {relative}")
    return target


def apply_proposal(
    repo: Path, proposal: PatchProposal, project: ProjectConfig, limits: ExperimentConfig
) -> tuple[str, list[str]]:
    if not proposal.edits:
        raise PatchError("Proposta sem alterações")
    paths = list(dict.fromkeys(edit.path for edit in proposal.edits))
    if len(paths) > limits.max_files_per_patch:
        raise PatchError("Limite de arquivos excedido")
    if (
        sum(len(edit.old_text) + len(edit.new_text) for edit in proposal.edits)
        > limits.max_patch_chars
    ):
        raise PatchError("Limite de tamanho de patch excedido")
    tracked = set(filter(None, git(repo, "ls-files", "-z").split("\0")))
    originals: dict[str, str] = {}
    updates: dict[str, str] = {}
    for edit in proposal.edits:
        path = safe_path(repo, edit.path)
        if edit.path not in tracked or not path.is_file():
            raise PatchError("Somente arquivos existentes e versionados podem ser alterados")
        if not matches(edit.path, project.allowed_edit_globs) or matches(
            edit.path, project.protected_globs
        ):
            raise PatchError(f"Arquivo protegido ou fora do escopo: {edit.path}")
        original = originals.setdefault(edit.path, path.read_bytes().decode("utf-8"))
        current = updates.get(edit.path, original)
        if not edit.old_text or current.count(edit.old_text) != 1:
            raise PatchError(f"Trecho antigo deve corresponder exatamente uma vez em {edit.path}")
        if edit.old_text == edit.new_text:
            raise PatchError("Alteração vazia")
        # A repair may not hide analysis results instead of correcting the code.
        suppressions = (
            "NOSONAR",
            "@SuppressWarnings",
            "sonar.issue.ignore",
            "sonar.exclusions",
            "noqa",
            "type: ignore",
        )
        if any(
            edit.new_text.count(marker) > edit.old_text.count(marker) for marker in suppressions
        ):
            raise PatchError("Supressão de análise introduzida pelo patch")
        updates[edit.path] = current.replace(edit.old_text, edit.new_text, 1)
    diff = "".join(
        "".join(
            difflib.unified_diff(
                originals[name].splitlines(True),
                updates[name].splitlines(True),
                fromfile=f"a/{name}",
                tofile=f"b/{name}",
            )
        )
        for name in paths
    )
    if not diff:
        raise PatchError("Patch sem diferença líquida")
    try:
        for name in paths:
            safe_path(repo, name).write_bytes(updates[name].encode("utf-8"))
    except OSError:
        for name, original in originals.items():
            safe_path(repo, name).write_bytes(original.encode("utf-8"))
        raise
    return diff, paths
