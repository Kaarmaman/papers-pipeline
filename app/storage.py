from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from .config import Config


UTC = timezone.utc


def _as_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


class MongoStore:
    """MongoDB persistence for state, paper records, and run diagnostics.

    PyMongo is imported lazily so local ranking tests do not need a live MongoDB
    server or the optional production dependency.
    """

    def __init__(self, config: Config):
        if not config.mongo_uri:
            raise RuntimeError("MONGO_URI is required; configure the Portainer MongoDB connection")
        self._run_retention_days = config.mongo_run_retention_days
        try:
            from pymongo import MongoClient
            from pymongo.errors import PyMongoError
        except ImportError as exc:
            raise RuntimeError("pymongo is required for MongoDB storage") from exc

        self._pymongo_error = PyMongoError
        try:
            self.client = MongoClient(
                config.mongo_uri,
                serverSelectionTimeoutMS=config.mongo_server_selection_timeout_ms,
                appname="interesting-papers",
            )
            self.client.admin.command("ping")
            database = self.client[config.mongo_database]
            self.state = database["state"]
            self.papers = database["papers"]
            self.runs = database["runs"]
            self.papers.create_index("doi", sparse=True)
            self.papers.create_index("published_date")
            self.runs.create_index("finished_at")
            if config.mongo_run_retention_days > 0:
                self.runs.create_index("expires_at", expireAfterSeconds=0)
        except Exception as exc:
            self.close()
            if isinstance(exc, PyMongoError):
                raise RuntimeError(f"MongoDB connection failed: {type(exc).__name__}") from exc
            raise

    def get_state(self, key: str) -> str | None:
        document = self.state.find_one({"_id": key}, {"value": 1})
        return str(document["value"]) if document and document.get("value") is not None else None

    def set_state(self, key: str, value: str) -> None:
        self.state.update_one(
            {"_id": key},
            {"$set": {"value": value, "updated_at": datetime.now(UTC)}},
            upsert=True,
        )

    def save_papers(self, papers: Iterable[dict[str, Any]], seen_at: str) -> None:
        timestamp = _as_datetime(seen_at)
        for paper in papers:
            key = str(paper["key"])
            document = dict(paper)
            document.pop("key", None)
            document["_id"] = key
            document["last_seen_at"] = timestamp
            self.papers.update_one(
                {"_id": key},
                {
                    "$set": document,
                    "$setOnInsert": {"first_seen_at": timestamp},
                },
                upsert=True,
            )

    def list_papers(self) -> list[dict[str, Any]]:
        papers = []
        for document in self.papers.find({}):
            paper = dict(document)
            paper["key"] = str(paper.pop("_id"))
            for field in ("first_seen_at", "last_seen_at"):
                value = paper.get(field)
                if isinstance(value, datetime):
                    paper[field] = value.astimezone(UTC).isoformat().replace("+00:00", "Z")
            papers.append(paper)
        return papers

    def save_run(
        self,
        started: str,
        finished: str,
        cutoff: str,
        success: bool,
        count: int,
        errors: dict[str, str],
    ) -> None:
        finished_at = _as_datetime(finished)
        document: dict[str, Any] = {
            "started_at": _as_datetime(started),
            "finished_at": finished_at,
            "cutoff_at": _as_datetime(cutoff),
            "success": bool(success),
            "papers_found": int(count),
            "errors": errors,
        }
        retention = getattr(self, "_run_retention_days", 0)
        if retention > 0:
            document["expires_at"] = finished_at + timedelta(days=retention)
        self.runs.insert_one(document)

    def reset(self) -> dict[str, int]:
        return {
            name: getattr(self, name).delete_many({}).deleted_count
            for name in ("state", "papers", "runs")
        }

    def close(self) -> None:
        client = getattr(self, "client", None)
        if client is not None:
            client.close()
