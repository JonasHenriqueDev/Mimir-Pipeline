"""Private repository copy and detached worktrees; never mutate the input repository."""

import subprocess
from pathlib import Path


class WorkspaceError(RuntimeError):
    pass


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.quotepath=false",
            "-C",
            str(repo),
            *args,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    if result.returncode:
        raise WorkspaceError(result.stderr.strip() or "Git falhou")
    # NUL-delimited path output must preserve whitespace in filenames.
    return result.stdout if "-z" in args else result.stdout.strip()


def resolve_commit(repo: Path, ref: str) -> str:
    if not repo.is_dir():
        raise WorkspaceError(f"Repositório local não encontrado: {repo}")
    return git(repo, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}")


class Workspace:
    def __init__(self, source: Path, directory: Path, base_commit: str):
        self.directory = directory.resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.repo = self.directory / "repository"
        if self.repo.exists():
            raise WorkspaceError(f"Diretório já existe: {self.repo}")
        result = subprocess.run(
            [
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "clone",
                "--no-hardlinks",
                "--no-checkout",
                "--",
                str(source.resolve()),
                str(self.repo),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        if result.returncode:
            raise WorkspaceError(result.stderr.strip())
        self.base_commit = base_commit
        git(self.repo, "config", "core.autocrlf", "false")
        git(self.repo, "config", "core.hooksPath", "/dev/null")
        git(self.repo, "config", "user.name", "Mimir Pipeline")
        git(self.repo, "config", "user.email", "pipeline@localhost")
        git(self.repo, "config", "commit.gpgsign", "false")

    def checkout(self, name: str, commit: str) -> Path:
        path = (self.directory / "worktrees" / name).resolve()
        if not path.is_relative_to(self.directory / "worktrees") or path.exists():
            raise WorkspaceError("Destino de worktree inválido ou já existente")
        path.parent.mkdir(parents=True, exist_ok=True)
        git(self.repo, "worktree", "add", "--detach", str(path), commit)
        return path

    def commit(self, path: Path, paths: list[str], message: str) -> str:
        git(path, "add", "--", *paths)
        git(path, "-c", "commit.gpgsign=false", "commit", "-m", message)
        return git(path, "rev-parse", "HEAD")


def tracked_changes(repo: Path) -> set[str]:
    return set(filter(None, git(repo, "diff", "--name-only", "-z", "HEAD").split("\0")))
