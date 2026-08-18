import unittest
from datetime import datetime, timezone
from tempfile import TemporaryDirectory
from pathlib import Path
import sys
import types
import os
from unittest.mock import patch

from app.config import Config
from app.pipeline import canonicalize, merge_papers, run_once, score_paper, SearchResult


UTC = timezone.utc


class PipelineTests(unittest.TestCase):
    def test_config_accepts_neutral_llm_settings_and_normalizes_mongo_host(self):
        with patch.dict(os.environ, {
            "MONGO_URI": "192.168.1.26:27017",
            "LLM_BASE_URL": "https://integrate.api.nvidia.com/v1",
            "LLM_MODEL": "nvidia/test-model",
            "LLM_API_KEY": "test-key",
        }, clear=True):
            config = Config.from_env()
        self.assertEqual(config.mongo_uri, "mongodb://192.168.1.26:27017")
        self.assertEqual(config.llm_base_url, "https://integrate.api.nvidia.com/v1")
        self.assertEqual(config.llm_model, "nvidia/test-model")
        self.assertEqual(config.llm_api_key, "test-key")

    def test_canonical_key_prefers_doi(self):
        paper = canonicalize({"title": "A paper", "doi": "10.1234/ABC", "published_date": "2026-08-17"}, "Bitcoin")
        self.assertEqual(paper["key"], "doi:10.1234/abc")

    def test_merge_deduplicates_across_topics_and_keeps_new_date(self):
        results = [
            SearchResult("Bitcoin", {"title": "Digital asset markets", "doi": "10/abc", "published_date": "2026-08-17", "abstract": "bitcoin market"}),
            SearchResult("market structure", {"title": "Digital asset markets", "doi": "10/abc", "published_date": "2026-08-17", "abstract": "liquidity and market structure"}),
        ]
        papers, missing = merge_papers(results, datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 8, 18, tzinfo=UTC))
        self.assertEqual(missing, 0)
        self.assertEqual(len(papers), 1)
        self.assertEqual(set(papers[0]["topics"]), {"Bitcoin", "market structure"})

    def test_old_papers_are_excluded(self):
        results = [SearchResult("energy", {"title": "Old", "published_date": "2026-07-01"})]
        papers, _ = merge_papers(results, datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 8, 18, tzinfo=UTC))
        self.assertEqual(papers, [])

    def test_relevance_is_bounded_and_topic_sensitive(self):
        paper = canonicalize({
            "title": "Asset allocation and inflation risk in Bitcoin portfolios",
            "abstract": "We study portfolio returns, volatility, diversification and monetary policy.",
            "published_date": "2026-08-17",
            "doi": "10/abc",
        }, "asset allocation")
        scored = score_paper(paper, datetime(2026, 8, 18, tzinfo=UTC))
        self.assertGreaterEqual(scored["relevance_score"], 40)
        self.assertLessEqual(scored["relevance_score"], 100)
        self.assertIn("asset allocation", scored["matched_topics"])

    def test_run_once_persists_cutoff_and_reports(self):
        with TemporaryDirectory() as directory:
            config = Config(
                data_dir=Path(directory), search_command="unused", search_sources="crossref",
                max_results_per_source=1, search_timeout_seconds=30, lookback_days=30,
                check_interval_hours=168, run_on_startup=True, fetch_fulltext=False,
                max_fulltext_chars=4000, llm_base_url="https://example.invalid/v1",
                llm_api_key="", llm_model="test", llm_timeout_seconds=30,
            )
            raw = {"title": "Fresh portfolio research", "doi": "10/example", "published_date": datetime.now(UTC).date().isoformat(), "abstract": "portfolio returns and risk"}
            class FakeStore:
                instances = []

                def __init__(self, _config):
                    self.state = {}
                    self.saved = []
                    self.runs = []
                    FakeStore.instances.append(self)

                def get_state(self, key):
                    return self.state.get(key)

                def set_state(self, key, value):
                    self.state[key] = value

                def save_papers(self, papers, _seen_at):
                    self.saved.extend(papers)

                def save_run(self, *args):
                    self.runs.append(args)

                def close(self):
                    pass

            with patch("app.pipeline.Store", FakeStore), patch("app.pipeline.run_search", return_value=([raw], None)):
                report = run_once(config)
            self.assertTrue(report["success"])
            self.assertEqual(report["paper_count"], 1)
            self.assertTrue((Path(directory) / "reports" / "latest.html").exists())
            self.assertTrue((Path(directory) / "reports" / "latest.json").exists())
            self.assertEqual(len(FakeStore.instances[0].saved), 1)

    def test_mongo_store_requires_uri(self):
        from app.storage import MongoStore

        config = Config(
            data_dir=Path("."), search_command="unused", search_sources="crossref",
            max_results_per_source=1, search_timeout_seconds=30, lookback_days=30,
            check_interval_hours=168, run_on_startup=True, fetch_fulltext=False,
            max_fulltext_chars=4000, llm_base_url="https://example.invalid/v1",
            llm_api_key="", llm_model="test", llm_timeout_seconds=30,
        )
        with self.assertRaisesRegex(RuntimeError, "MONGO_URI is required"):
            MongoStore(config)

    def test_mongo_store_writes_state_paper_and_run(self):
        class Collection:
            def __init__(self):
                self.documents = {}
                self.indexes = []

            def create_index(self, *args, **kwargs):
                self.indexes.append((args, kwargs))

            def find_one(self, query, _projection=None):
                return self.documents.get(query["_id"])

            def update_one(self, query, update, upsert=False):
                document = self.documents.setdefault(query["_id"], {"_id": query["_id"]})
                document.update(update.get("$set", {}))
                document.update(update.get("$setOnInsert", {}))

            def insert_one(self, document):
                self.documents[str(len(self.documents))] = document

        class Database:
            def __init__(self):
                self.collections = {}

            def __getitem__(self, name):
                return self.collections.setdefault(name, Collection())

        class Client:
            def __init__(self, *_args, **_kwargs):
                self.database = Database()
                self.admin = self

            def command(self, _name):
                return {"ok": 1}

            def __getitem__(self, _name):
                return self.database

            def close(self):
                pass

        fake_pymongo = types.ModuleType("pymongo")
        fake_pymongo.MongoClient = Client
        fake_errors = types.ModuleType("pymongo.errors")
        fake_errors.PyMongoError = RuntimeError
        config = Config(
            data_dir=Path("."), search_command="unused", search_sources="crossref",
            max_results_per_source=1, search_timeout_seconds=30, lookback_days=30,
            check_interval_hours=168, run_on_startup=True, fetch_fulltext=False,
            max_fulltext_chars=4000, llm_base_url="https://example.invalid/v1",
            llm_api_key="", llm_model="test", llm_timeout_seconds=30,
            mongo_uri="mongodb://example", mongo_database="test_db",
        )
        paper = {"key": "doi:10/example", "title": "Test", "relevance_score": 82}
        with patch.dict(sys.modules, {"pymongo": fake_pymongo, "pymongo.errors": fake_errors}):
            from app.storage import MongoStore
            store = MongoStore(config)
            store.set_state("last_success_at", "2026-08-18T00:00:00Z")
            store.save_papers([paper], "2026-08-18T01:00:00Z")
            store.save_run("2026-08-18T01:00:00Z", "2026-08-18T01:01:00Z", "2026-08-17T00:00:00Z", True, 1, {})
            self.assertEqual(store.get_state("last_success_at"), "2026-08-18T00:00:00Z")
            self.assertIn("doi:10/example", store.papers.documents)
            self.assertEqual(len(store.runs.documents), 1)
            store.close()


if __name__ == "__main__":
    unittest.main()
