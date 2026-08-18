from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_first(names: tuple[str, ...], default: str = "") -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return default


@dataclass(frozen=True)
class Config:
    data_dir: Path
    search_command: str
    search_sources: str
    max_results_per_source: int
    search_timeout_seconds: int
    lookback_days: int
    check_interval_hours: int
    run_on_startup: bool
    fetch_fulltext: bool
    max_fulltext_chars: int
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    llm_timeout_seconds: int
    mongo_uri: str = ""
    mongo_database: str = "interesting_papers"
    mongo_server_selection_timeout_ms: int = 10000
    mongo_run_retention_days: int = 365
    keep_fulltext: bool = False

    @classmethod
    def from_env(cls) -> "Config":
        mongo_uri = _env_first(("MONGO_URI",))
        if mongo_uri and "://" not in mongo_uri:
            mongo_uri = f"mongodb://{mongo_uri}"
        return cls(
            data_dir=Path(os.getenv("DATA_DIR", "/data")),
            search_command=os.getenv("PAPER_SEARCH_COMMAND", "paper-search"),
            search_sources=os.getenv(
                "PAPER_SEARCH_SOURCES",
                "crossref,openalex,semantic,arxiv,ssrn,zenodo,hal",
            ),
            max_results_per_source=max(1, _int("MAX_RESULTS_PER_SOURCE", 8)),
            search_timeout_seconds=max(30, _int("SEARCH_TIMEOUT_SECONDS", 180)),
            lookback_days=max(1, _int("LOOKBACK_DAYS", 30)),
            check_interval_hours=max(1, _int("CHECK_INTERVAL_HOURS", 168)),
            run_on_startup=_bool("RUN_ON_STARTUP", True),
            fetch_fulltext=_bool("FETCH_FULLTEXT", True),
            max_fulltext_chars=max(4000, _int("MAX_FULLTEXT_CHARS", 28000)),
            keep_fulltext=_bool("KEEP_FULLTEXT", False),
            llm_base_url=_env_first(("LLM_BASE_URL",), "https://api.openai.com/v1").rstrip("/"),
            llm_api_key=_env_first(("LLM_API_KEY",)),
            llm_model=_env_first(("LLM_MODEL",), "gpt-4.1-mini"),
            llm_timeout_seconds=max(30, _int("LLM_TIMEOUT_SECONDS", 120)),
            mongo_uri=mongo_uri,
            mongo_database=os.getenv("MONGO_DATABASE", "interesting_papers").strip() or "interesting_papers",
            mongo_server_selection_timeout_ms=max(1000, _int("MONGO_SERVER_SELECTION_TIMEOUT_MS", 10000)),
            mongo_run_retention_days=max(0, _int("MONGO_RUN_RETENTION_DAYS", 365)),
        )

    @property
    def report_dir(self) -> Path:
        return self.data_dir / "reports"

    @property
    def download_dir(self) -> Path:
        return self.data_dir / "downloads"
