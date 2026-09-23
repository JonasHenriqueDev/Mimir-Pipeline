"""Bounded process execution. Repository commands are trusted researcher configuration."""

import os
import signal
import subprocess
import sys
import time
import uuid
import warnings
from pathlib import Path

from .config import RunnerConfig
from .models import CommandResult
from .workspace import git


class RunnerError(RuntimeError):
    pass


def clean_environment() -> dict[str, str]:
    # Do not give repository tests LLM credentials, cloud tokens or Sonar credentials.
    allowed = {
        "PATH",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
        "TEMP",
        "TMP",
        "HOME",
        "USERPROFILE",
        "LOCALAPPDATA",
        "APPDATA",
        "JAVA_HOME",
        "LANG",
        "LC_ALL",
    }
    result = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    result.update({"PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8", "CI": "true"})
    return result


def stop_process(process: subprocess.Popen) -> None:
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                timeout=15,
                env=clean_environment(),
            )
        except (OSError, subprocess.TimeoutExpired):
            pass  # Still attempt a direct kill when taskkill is unavailable.
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.kill()
    process.wait(timeout=10)


def bounded_process(
    command: list[str], cwd: Path, log: Path, timeout: float, env: dict[str, str] | None = None
) -> CommandResult:
    log.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    timed_out = False
    with log.open("w", encoding="utf-8") as stream:
        try:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                stdout=stream,
                stderr=subprocess.STDOUT,
                env=env or clean_environment(),
                stdin=subprocess.DEVNULL,
                start_new_session=os.name != "nt",
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
            )
        except OSError as exc:
            raise RunnerError(f"Não foi possível iniciar {command[0]}: {exc}") from exc
        try:
            process.wait(timeout=max(0.01, timeout))
        except subprocess.TimeoutExpired:
            timed_out = True
            stop_process(process)
        except KeyboardInterrupt:
            try:
                stop_process(process)
            except (OSError, subprocess.SubprocessError) as exc:
                warnings.warn(
                    f"Falha ao encerrar processo interrompido: {exc}", RuntimeWarning, stacklevel=2
                )
            raise
    return CommandResult(
        command=command,
        returncode=process.returncode,
        duration_seconds=round(time.monotonic() - started, 3),
        timed_out=timed_out,
        log_path=str(log),
    )


class Runner:
    def __init__(self, config: RunnerConfig, repo: Path, logs: Path, deadline: float | None = None):
        self.config, self.repo, self.logs = config, repo, logs
        self.container: str | None = None
        self.deadline = deadline
        self.ready = False

    def timeout(self) -> float:
        remaining = self.config.timeout_seconds
        if self.deadline is not None:
            remaining = min(remaining, self.deadline - time.monotonic())
        if remaining <= 0:
            raise RunnerError("Limite de tempo do experimento atingido")
        return remaining

    def docker(self, args: list[str]) -> str:
        try:
            result = subprocess.run(
                ["docker", *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout(),
                env=clean_environment(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RunnerError(f"Falha ao executar Docker: {exc}") from exc
        if result.returncode:
            raise RunnerError(result.stderr.strip() or "Docker retornou falha")
        return result.stdout.strip()

    def __enter__(self):
        if self.config.mode == "docker":
            self.container = "tcc-" + uuid.uuid4().hex[:16]
            common = Path(git(self.repo, "rev-parse", "--git-common-dir"))
            common = (common if common.is_absolute() else self.repo / common).resolve()
            git_dir = Path(git(self.repo, "rev-parse", "--absolute-git-dir")).resolve()
            inside_git = "/git/" + git_dir.relative_to(common).as_posix()
            args = [
                "create",
                "--name",
                self.container,
                "--network",
                self.config.setup_network,
                "--memory",
                self.config.memory,
                "--cpus",
                str(self.config.cpus),
                "--pids-limit",
                "256",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                "--mount",
                f"type=bind,source={self.repo},target=/workspace",
                "--mount",
                f"type=bind,source={common},target=/git,readonly",
                "--workdir",
                "/workspace",
                "-e",
                f"GIT_DIR={inside_git}",
                "-e",
                "GIT_WORK_TREE=/workspace",
                "-e",
                "CI=true",
                "-e",
                "PYTHONDONTWRITEBYTECODE=1",
                "--entrypoint",
                "/bin/sh",
                self.config.image,
                "-c",
                "while :; do sleep 3600; done",
            ]
            try:
                self.docker(args)
                self.docker(["start", self.container])
            except Exception:
                self.close()
                raise
        return self

    def finish_setup(self) -> None:
        if self.ready:
            return
        if self.container and self.config.setup_network != self.config.network:
            self.docker(["network", "disconnect", self.config.setup_network, self.container])
            if self.config.network != "none":
                self.docker(["network", "connect", self.config.network, self.container])
        self.ready = True

    def run(self, command: list[str], name: str) -> CommandResult:
        if not command or any(not isinstance(item, str) or not item for item in command):
            raise RunnerError("Comando deve ser uma lista não vazia de argumentos")
        expanded = [
            item.replace("{python}", "python" if self.container else sys.executable).replace(
                "{repo}", "/workspace" if self.container else str(self.repo)
            )
            for item in command
        ]
        actual = ["docker", "exec", self.container, *expanded] if self.container else expanded
        result = bounded_process(actual, self.repo, self.logs / f"{name}.log", self.timeout())
        if result.timed_out and self.container:
            self.close()
        result.command = expanded
        return result

    def close(self) -> None:
        if self.container:
            name = self.container
            try:
                outcome = subprocess.run(
                    ["docker", "rm", "-f", name],
                    capture_output=True,
                    timeout=30,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=clean_environment(),
                )
                if outcome.returncode:
                    warnings.warn(
                        f"Não foi possível remover o contêiner {name}: {outcome.stderr.strip()}",
                        RuntimeWarning,
                        stacklevel=2,
                    )
            except (OSError, subprocess.SubprocessError) as exc:
                warnings.warn(
                    f"Não foi possível remover o contêiner {name}: {exc}",
                    RuntimeWarning,
                    stacklevel=2,
                )
            finally:
                self.container = None

    def __exit__(self, *args):
        self.close()
