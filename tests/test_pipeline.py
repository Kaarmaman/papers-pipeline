import unittest
import json
from datetime import datetime, timezone
from tempfile import TemporaryDirectory
from pathlib import Path
import sys
import types
import os
from unittest.mock import patch

from app.config import Config
from app.pipeline import canonicalize, main, merge_papers, notify_discord, reset_and_run, run_once, score_paper, SearchResult


UTC = timezone.utc


class PipelineTests(unittest.TestCase):
    def test_config_accepts_neutral_llm_settings_and_normalizes_mongo_host(self):
        with patch.dict(os.environ, {
            "MONGO_URI": "192.168.1.26:27017",
            "LLM_BASE_URL": "https://integrate.api.nvidia.com/v1",
            "LLM_MODEL": "nvidia/test-model",
            "LLM_API_KEY": "test-key",
            "DISCORD_WEBHOOK_URL": "https://discord.example/webhook",
            "PAPER_REPORT_URL": "http://192.168.1.26:8099/",
        }, clear=True):
            config = Config.from_env()
        self.assertEqual(config.mongo_uri, "mongodb://192.168.1.26:27017")
        self.assertEqual(config.llm_base_url, "https://integrate.api.nvidia.com/v1")
        self.assertEqual(config.llm_model, "nvidia/test-model")
        self.assertEqual(config.llm_api_key, "test-key")
        self.assertEqual(config.discord_webhook_url, "https://discord.example/webhook")
        self.assertEqual(config.paper_report_url, "http://192.168.1.26:8099/")

    def test_discord_notification_contains_top_five_and_does_not_fail_run(self):
        config = Config(
            data_dir=Path("."), search_command="unused", search_sources="crossref",
            max_results_per_source=1, search_timeout_seconds=30, lookback_days=30,
            check_interval_hours=168, run_on_startup=True, fetch_fulltext=False,
            max_fulltext_chars=4000, llm_base_url="https://example.invalid/v1",
            llm_api_key="", llm_model="test", llm_timeout_seconds=30,
            discord_webhook_url="https://discord.example/webhook",
            paper_report_url="http://192.168.1.26:8099/",
        )
        captured = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            captured["timeout"] = timeout
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            return Response()

        report = {
            "success": True,
            "paper_count": 1,
            "checked_at": "2026-08-18T00:00:00Z",
            "top5": [{"paper": {"title": "A paper", "relevance_score": 91, "doi": "10/example"}}],
        }
        with patch("app.pipeline.urllib.request.urlopen", side_effect=fake_urlopen):
            notify_discord(config, report)
        self.assertEqual(captured["url"], "https://discord.example/webhook")
        self.assertEqual(captured["timeout"], 15)
        self.assertIn("A paper", captured["payload"]["content"])
        self.assertIn("http://192.168.1.26:8099/", captured["payload"]["content"])
        self.assertEqual(captured["payload"]["allowed_mentions"], {"parse": []})

    def test_canonical_key_prefers_doi(self):
        paper = canonicalize({"title": "A paper", "doi": "10.1234/ABC", "published_date": "2026-08-17"}, "Bitcoin")
        self.assertEqual(paper["key"], "doi:10.1234/abc")

    def test_merge_deduplicates_across_topics_and_keeps_new_date(self):
        results = [
            SearchResult("Bitcoin", {"title": "Digital asset markets", "doi": "10/abc", "published_date": "2026-08-17", "abstract": "bitcoin market"}),
            SearchResult("market structure", {"title": "Digital asset markets", "doi": "10/abc", "published_date": "2026-08-17", "abstract": "liquidity and market structure"}),
        ]
        papers, counts = merge_papers(results, datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 8, 18, tzinfo=UTC))
        self.assertEqual(counts["missing_date"], 0)
        self.assertEqual(len(papers), 1)
        self.assertEqual(set(papers[0]["topics"]), {"Bitcoin", "market structure"})

    def test_old_papers_are_excluded(self):
        results = [SearchResult("energy", {"title": "Old", "published_date": "2026-07-01"})]
        papers, _ = merge_papers(results, datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 8, 18, tzinfo=UTC))
        self.assertEqual(papers, [])

    def test_merge_reports_date_rejection_counts(self):
        results = [
            SearchResult("energy", {"title": "Missing date"}),
            SearchResult("energy", {"title": "Old", "published_date": "2026-07-01"}),
            SearchResult("energy", {"title": "Future", "published_date": "2026-08-21"}),
        ]
        papers, counts = merge_papers(
            results,
            datetime(2026, 8, 1, tzinfo=UTC),
            datetime(2026, 8, 18, tzinfo=UTC),
        )
        self.assertEqual(papers, [])
        self.assertEqual(counts, {"missing_date": 1, "before_cutoff": 1, "future_date": 1})

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

    def test_reset_and_run_clears_reports_before_fresh_run(self):
        with TemporaryDirectory() as directory:
            report_dir = Path(directory) / "reports"
            report_dir.mkdir()
            for name in ("latest.html", "latest.json", ".latest.html.tmp", ".latest.json.tmp"):
                (report_dir / name).write_text("stale", encoding="utf-8")
            config = Config(
                data_dir=Path(directory), search_command="unused", search_sources="crossref",
                max_results_per_source=1, search_timeout_seconds=30, lookback_days=30,
                check_interval_hours=168, run_on_startup=True, fetch_fulltext=False,
                max_fulltext_chars=4000, llm_base_url="https://example.invalid/v1",
                llm_api_key="", llm_model="test", llm_timeout_seconds=30,
            )
            events = []

            class FakeStore:
                def __init__(self, _config):
                    pass

                def reset(self):
                    events.append(("reset", sorted(path.name for path in report_dir.iterdir())))
                    return {"state": 1, "papers": 2, "runs": 3}

                def close(self):
                    pass

            def fake_run_once(_config, _store):
                events.append(("run", sorted(path.name for path in report_dir.iterdir())))
                return {"success": True, "paper_count": 4, "errors": {}}

            with patch("app.pipeline.Store", FakeStore), patch("app.pipeline._run_once", fake_run_once):
                report = reset_and_run(config)

            self.assertEqual(report["paper_count"], 4)
            self.assertEqual(events, [("reset", []), ("run", [])])

    def test_main_reset_requires_confirmation(self):
        with patch.object(sys, "argv", ["pipeline", "--reset-and-run"]):
            with self.assertRaises(SystemExit) as raised:
                main()
        self.assertEqual(raised.exception.code, 2)

    def test_main_reset_applies_lookback_override(self):
        captured = {}

        def fake_reset_and_run(config):
            captured["lookback_days"] = config.lookback_days
            return {"success": True, "paper_count": 0, "errors": {}}

        with patch.object(sys, "argv", ["pipeline", "--reset-and-run", "--confirm-reset", "--lookback-days", "365"]), \
                patch("app.pipeline.reset_and_run", fake_reset_and_run):
            self.assertEqual(main(), 0)
        self.assertEqual(captured["lookback_days"], 365)

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

            def delete_many(self, _query):
                deleted = len(self.documents)
                self.documents.clear()
                return types.SimpleNamespace(deleted_count=deleted)

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
            self.assertEqual(store.reset(), {"state": 1, "papers": 1, "runs": 1})
            self.assertEqual(store.state.documents, {})
            self.assertEqual(store.papers.documents, {})
            self.assertEqual(store.runs.documents, {})
            store.close()


if __name__ == "__main__":
    unittest.main()
