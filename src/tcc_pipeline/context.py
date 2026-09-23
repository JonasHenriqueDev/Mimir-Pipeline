"""Bounded, deterministic source context. Repository contents are untrusted data."""

import json
import os
import re
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from .config import ProjectConfig
from .models import Issue

_EXCLUDED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    "dist",
    "build",
    "target",
    "artifacts",
    "secrets",
    ".aws",
    ".ssh",
    "reference_labels",
    "ground_truth",
}
_SECRET_NAME = re.compile(
    r"(^\.env|credentials|secrets?|private[_-]?key|id_rsa|id_ed25519|"
    r"ground[_-]?truth|reference[_-]?labels|manual[_-]?labels)",
    re.I,
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)((?:[\"']?)(?:[\w-]*(?:api[_-]?key|access[_-]?token|auth[_-]?token|"
    r"secret|password|passwd)[\w-]*)(?:[\"']?)\s*[:=]\s*)([\"'])([^\r\n]*?)\2"
)


def redact_text(text: str, secrets: tuple[str, ...] = ()) -> str:
    """Best-effort redaction, also applied before sending context to a provider."""
    text = re.sub(
        r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
        # Preserve source line numbering and newline style for issue anchors.
        lambda match: "[REDACTED PRIVATE KEY]" + "".join(re.findall(r"\r\n|\r|\n", match.group())),
        text,
        flags=re.S,
    )
    text = _SECRET_ASSIGNMENT.sub(r"\1\2[REDACTED]\2", text)
    text = re.sub(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{16,}\b", "[REDACTED]", text)
    text = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{8,}=*", "Bearer [REDACTED]", text)
    for value in secrets:
        if value:
            text = text.replace(value, "[REDACTED]")
    return text


def _safe_path(repo: Path, relative: str) -> Path:
    path = PurePosixPath(relative.replace("\\", "/"))
    if (
        not relative
        or ":" in relative
        or "\x00" in relative
        or path.is_absolute()
        or PureWindowsPath(relative).drive
        or ".." in path.parts
    ):
        raise ValueError(f"Caminho de contexto fora do repositório: {relative}")
    if any(part.lower() in _EXCLUDED_DIRS for part in path.parts):
        raise ValueError(f"Diretório excluído do contexto: {relative}")
    if _SECRET_NAME.search(path.name) or path.suffix.lower() in {".pem", ".key", ".p12", ".pfx"}:
        raise ValueError(f"Arquivo sensível excluído do contexto: {relative}")
    target = repo / path
    resolved = target.resolve()
    if not resolved.is_relative_to(repo):
        raise ValueError(f"Link fora do repositório: {relative}")
    for part in [target, *target.parents]:
        if part == repo:
            break
        if _is_link(part):
            raise ValueError(f"Link não permitido no contexto: {relative}")
    return resolved


def _is_link(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def _is_test(path: Path) -> bool:
    name = path.name.lower()
    return (
        name.startswith("test_")
        or name.endswith("_test.py")
        or name.endswith("test.java")
        or ".test." in name
        or ".spec." in name
    )


def _nearby_tests(repo: Path, target: Path) -> list[str]:
    candidates: list[tuple[int, str]] = []
    stem = target.stem.lower()
    for directory, dirs, files in os.walk(repo, followlinks=False):
        dirs[:] = sorted(
            d for d in dirs if d.lower() not in _EXCLUDED_DIRS and not _is_link(Path(directory) / d)
        )
        for filename in sorted(files):
            path = Path(directory) / filename
            if not _is_test(path):
                continue
            relative = path.relative_to(repo).as_posix()
            shared = len(set(path.parent.parts) & set(target.parent.parts))
            score = (1000 if stem in path.stem.lower() else 0) + shared
            candidates.append((score, relative))
            if len(candidates) >= 2000:
                break
        if len(candidates) >= 2000:
            break
    return [name for _, name in sorted(candidates, key=lambda item: (-item[0], item[1]))[:6]]


def _snippet(text: str, line: int, limit: int) -> tuple[str, dict[str, Any]]:
    lines = text.splitlines(keepends=True)
    if not lines:
        return "", {"start_line": 1, "end_line": 1, "truncated": False}
    center = min(max(line - 1, 0), len(lines) - 1)
    if len(lines[center]) > limit:
        raise ValueError("A linha alvo excede o limite de contexto; aumente max_context_chars.")
    start, end = center, center + 1
    count = len(lines[center])
    while start > 0 or end < len(lines):
        changed = False
        if start > 0 and count + len(lines[start - 1]) <= limit:
            start -= 1
            count += len(lines[start])
            changed = True
        if end < len(lines) and count + len(lines[end]) <= limit:
            count += len(lines[end])
            end += 1
            changed = True
        if not changed:
            break
    return "".join(lines[start:end]), {
        "start_line": start + 1,
        "end_line": end,
        "truncated": start != 0 or end != len(lines),
    }


def build_context(repo: Path, issue: Issue, project: ProjectConfig, max_chars: int) -> dict:
    """Build JSON-bounded context, preserving exact line endings for edit matching.

    Test discovery is a deterministic filename/proximity heuristic. Explicit
    ``context_files`` take precedence. Sensitive paths and reference labels are
    excluded even if explicitly listed. Redaction is best effort, not a scanner.
    """
    repo = repo.resolve()
    target = _safe_path(repo, issue.path)
    if not target.is_file():
        raise ValueError(f"Arquivo alvo ausente: {issue.path}")
    if target.stat().st_size > 5_000_000:
        raise ValueError("Arquivo alvo excede 5 MB; refine o escopo de análise.")
    target_name = target.relative_to(repo).as_posix()
    issue_data = issue.model_dump(mode="json")
    for field in ("message", "rule_description", "anchor"):
        issue_data[field] = redact_text(issue_data[field])[: max(80, max_chars // 10)]
    context: dict[str, Any] = {
        "issue": issue_data,
        "language": project.language,
        "files": {},
        "file_metadata": {},
    }

    def size() -> int:
        return len(json.dumps(context, ensure_ascii=False))

    candidates = list(
        dict.fromkeys(
            [
                target_name,
                *project.context_files,
                *_nearby_tests(repo, target),
            ]
        )
    )
    for index, relative in enumerate(candidates):
        try:
            path = _safe_path(repo, relative)
        except ValueError:
            if index == 0:
                raise
            continue
        if not path.is_file() or path.stat().st_size > 5_000_000:
            continue
        try:
            raw = path.read_bytes().decode("utf-8")
        except (OSError, UnicodeError):
            if index == 0:
                raise ValueError("O arquivo alvo precisa ser texto UTF-8.") from None
            continue
        if "\x00" in raw:
            if index == 0:
                raise ValueError("O arquivo alvo contém dados binários.")
            continue
        name = path.relative_to(repo).as_posix()
        if name in context["files"]:
            continue
        text = redact_text(raw)
        remaining = max_chars - size() - 180 - len(name) * 2
        if remaining < 50:
            break
        # Leave room for declared dependencies and a test when the target is large.
        limit = max(50, int(remaining * 0.65)) if index == 0 and len(candidates) > 1 else remaining
        line = issue.line if index == 0 else 1
        try:
            snippet, metadata = _snippet(text, line, limit)
        except ValueError:
            if index == 0:
                raise
            continue
        context["files"][name] = snippet
        context["file_metadata"][name] = metadata
        # JSON escaping can take more space than raw source characters.
        while size() > max_chars:
            limit -= max(1, size() - max_chars)
            try:
                snippet, metadata = _snippet(text, line, limit)
            except ValueError:
                del context["files"][name]
                del context["file_metadata"][name]
                if index == 0:
                    raise ValueError("Contexto mínimo excede max_context_chars.") from None
                break
            context["files"][name] = snippet
            context["file_metadata"][name] = metadata
    if target_name not in context["files"]:
        raise ValueError("Contexto mínimo excede max_context_chars.")
    return context
