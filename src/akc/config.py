from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_env_file(env_path: Path) -> None:
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        os.environ.setdefault(key, value)


def _resolve_project_path(path: str | Path) -> Path:
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate
    return (PROJECT_ROOT / candidate).resolve()


@dataclass(frozen=True)
class Settings:
    openai_api_key: str
    model: str = "gpt-5.4"
    agentfs_db_path: str = ".agentfs/akc.db"

    @classmethod
    def from_env(cls, env_path: str | Path = ".env") -> "Settings":
        _load_env_file(_resolve_project_path(env_path))

        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not api_key:
            raise ValueError("OPENAI_API_KEY is required.")

        configured_agentfs_path = os.environ.get("AKC_AGENTFS_DB_PATH", ".agentfs/akc.db").strip()
        agentfs_path = _resolve_project_path(configured_agentfs_path or ".agentfs/akc.db")

        return cls(
            openai_api_key=api_key,
            model=os.environ.get("AKC_MODEL", "gpt-5.4").strip() or "gpt-5.4",
            agentfs_db_path=str(agentfs_path),
        )
