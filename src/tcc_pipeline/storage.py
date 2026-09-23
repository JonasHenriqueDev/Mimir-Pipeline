"""Append-only events, atomic checkpoints and queryable run catalog."""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import RunResult


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    temporary.replace(path)


def event(directory: Path, kind: str, **data: Any) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "events.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps({"at": utc_now(), "event": kind, **data}, ensure_ascii=False, default=str)
            + "\n"
        )


def save_result(run_dir: Path, result: RunResult, catalog: Path) -> None:
    payload = result.model_dump(mode="json")
    write_json(run_dir / "result.json", payload)
    catalog.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(catalog, timeout=30) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute(
            "CREATE TABLE IF NOT EXISTS runs (run_id TEXT, experiment_id TEXT, project TEXT, condition TEXT, status TEXT, base_commit TEXT, result_path TEXT, payload TEXT, PRIMARY KEY(experiment_id, run_id))"
        )
        db.execute(
            "INSERT OR REPLACE INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                result.run_id,
                result.experiment_id,
                result.project,
                result.condition,
                result.status,
                result.base_commit,
                str(run_dir / "result.json"),
                json.dumps(payload, ensure_ascii=False),
            ),
        )
