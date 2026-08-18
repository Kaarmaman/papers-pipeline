# Interesting papers to read

This is a separate Portainer stack that checks nine research priorities, remembers the last successful check, deduplicates new papers, ranks them from 0–100 for this reading profile, and publishes a compact HTML report. The top five receive a thesis, methodology, surprising result, weaknesses, and investor-implications section when an LLM provider is configured.

The retrieval layer uses the upstream [`openags/paper-search-mcp`](https://github.com/openags/paper-search-mcp) package and its `paper-search` CLI. Its free-first connectors include Crossref, OpenAlex, Semantic Scholar, arXiv, SSRN, Zenodo, HAL, and other academic sources. The pipeline does not bypass publisher access controls; full text is attempted only through the upstream tool's lawful/public paths.

## What is included

- `papers-worker`: scheduled retrieval, MongoDB state, ranking, optional open-full-text retrieval, and report generation.
- `papers-web`: read-only report server on port `8099` (`/`, `/api/latest`, `/healthz`).
- `papers-data`: named Docker volume containing the latest JSON/HTML reports and temporary downloads.

## What MongoDB stores

The worker uses the database named by `MONGO_DATABASE` (default `interesting_papers`) and three collections:

- `state`: the last successful check timestamp. This is what makes “since last check” reliable across container recreation.
- `papers`: one document per deduplicated DOI/title. It contains title, authors, abstract, DOI/URL, source and source ID, publication date, citation count, matched priorities, the 0–100 score and breakdown, full-text availability, first/last seen timestamps, and the optional top-five analysis. It does not contain API keys, Mongo credentials, or the downloaded PDF/full text.
- `runs`: one small diagnostic document per attempt with cutoff, start/end time, success, paper count, and source errors. These expire after 365 days by default through `MONGO_RUN_RETENTION_DAYS`.

The latest HTML and JSON report stays on the named `papers-data` volume because the web container serves it directly. Full-text downloads are used for analysis and deleted after each run by default (`KEEP_FULLTEXT=false`). Set that flag only if you explicitly want PDFs retained on disk; they are still not copied into MongoDB.

### Storage estimate

The default fan-out is 9 topics × 7 sources × 8 results, or at most 504 raw result envelopes per weekly run. Deduplication means MongoDB stores only unique papers. A realistic planning range is roughly 1–5 MB/week for 50–200 new papers including abstracts, metadata, scores, and indexes. A conservative upper budget is 10–25 MB/week if most of the 504 results are unique or abstracts are unusually large. Run diagnostics are negligible; retaining PDFs would add several MB per paper, which is why it is disabled by default.

Paper metadata is retained indefinitely unless explicitly pruned. The first run's `LOOKBACK_DAYS` controls the initial window; subsequent runs store only records newer than `last_success_at`.

The default schedule is once immediately and then weekly. `last_success_at` advances only after all topic searches return successfully, so a transient source failure causes the next run to retry the same cutoff instead of silently skipping a window.

The score is intentionally inspectable: priority-topic match (45), recency (20), investor fit (20), evidence quality (12), and cross-source breadth (3). It is a reading-priority heuristic, not a forecast, recommendation, or holdings-aware score.

## Portainer deployment

1. Copy `.env.example` to `.env` only for local Compose use. For Portainer, enter the same non-secret settings in the stack environment editor.
2. Set `MONGO_URI` to a MongoDB URI reachable from the worker container and keep `MONGO_DATABASE=interesting_papers` unless you want another database. If MongoDB is in another Stack, use its reachable host/IP or attach both stacks to the same explicitly shared Docker network; a service name from a different Stack is not automatically resolvable.
3. Deploy a new stack named `interesting-papers` from this repository using the repository deployment option so Portainer can build the included `Dockerfile`. The worker intentionally fails fast and remains unhealthy when `MONGO_URI` is missing or unreachable; it does not silently recreate SQLite storage.
4. Keep the optional search-provider secrets blank unless you have them. `PAPER_SEARCH_MCP_UNPAYWALL_EMAIL` is optional but enables Unpaywall; the other upstream keys improve limits or source coverage.
5. If you want the five full analyses, enter an LLM-compatible API key in `LLM_API_KEY`, plus `LLM_BASE_URL` and `LLM_MODEL` if needed. The key is read at runtime and never written to MongoDB or the report. Without it, the report explicitly uses metadata-only analysis and does not invent methodology or results.
6. Deploy and wait for `papers-worker` to complete its first run. Open `http://<docker-host>:8099/` and confirm the web container is healthy. The first run may take several minutes because it fans out across multiple public providers and may try public full text for the top five.

If Portainer is using the Web editor instead of Git, the stack still needs the image built from this repository. Build/push `interesting-papers:0.1.0` to a registry reachable by the Docker endpoint, then change both services from `build:` to that image and set `pull_policy: if_not_present` or the Portainer equivalent.

## Local validation

Use the bundled Python runtime on the development machine if `python` is not on `PATH`:

```powershell
& 'C:\Users\Admin\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m unittest discover -s tests -v
& 'C:\Users\Admin\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m compileall -q app tests
```

For a one-off run inside a started stack:

```bash
docker compose run --rm papers-worker python -m app.pipeline --once
```
