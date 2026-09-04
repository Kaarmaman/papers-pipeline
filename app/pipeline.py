from __future__ import annotations

import argparse
import html
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .config import Config
from .storage import MongoStore


UTC = timezone.utc
TOPICS: tuple[dict[str, Any], ...] = (
    {
        "name": "asset allocation",
        "query": "asset allocation portfolio diversification regime risk premia",
        "terms": ("asset allocation", "portfolio", "diversification", "risk parity", "regime"),
    },
    {
        "name": "monetary economics",
        "query": "monetary policy inflation interest rates central bank transmission",
        "terms": ("monetary", "inflation", "interest rate", "central bank", "policy transmission"),
    },
    {
        "name": "Bitcoin",
        "query": "Bitcoin monetary asset market liquidity adoption volatility",
        "terms": ("bitcoin", "cryptocurrency", "crypto", "digital asset", "blockchain"),
    },
    {
        "name": "energy",
        "query": "energy transition electricity prices oil gas commodities investment",
        "terms": ("energy", "electricity", "oil", "gas", "commodity", "energy transition"),
    },
    {
        "name": "demographics",
        "query": "demographic aging fertility migration labor force savings growth assets",
        "terms": ("demographic", "aging", "fertility", "migration", "labor force", "population"),
    },
    {
        "name": "geopolitical risk",
        "query": "geopolitical risk conflict sanctions trade fragmentation asset prices",
        "terms": ("geopolitical", "conflict", "sanction", "trade fragmentation", "war", "political risk"),
    },
    {
        "name": "market structure",
        "query": "market structure liquidity price discovery trading microstructure market impact",
        "terms": ("market structure", "liquidity", "price discovery", "microstructure", "market impact"),
    },
    {
        "name": "factor investing",
        "query": "factor investing value momentum quality profitability low volatility factor returns",
        "terms": ("factor", "value", "momentum", "quality", "profitability", "low volatility"),
    },
    {
        "name": "behavioral finance",
        "query": "behavioral finance investor beliefs attention sentiment behavioral bias asset prices",
        "terms": ("behavioral finance", "investor", "attention", "sentiment", "overconfidence", "bias"),
    },
)

INVESTOR_TERMS = (
    "return", "risk", "volatility", "portfolio", "allocation", "pricing", "premium",
    "liquidity", "drawdown", "hedge", "asset", "investment", "market", "factor",
    "forecast", "inflation", "interest rate", "policy", "yield", "transmission",
)


@dataclass
class SearchResult:
    topic: str
    payload: dict[str, Any]


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_date(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    text = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        match = re.search(r"(\d{4})[-/]?(\d{2})?[-/]?(\d{2})?", text)
        if not match:
            return None
        year, month, day = match.group(1), match.group(2) or "01", match.group(3) or "01"
        parsed = datetime(int(year), int(month), int(day))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def clean_text(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(clean_text(item) for item in value if item)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize_title(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def paper_key(paper: dict[str, Any]) -> str:
    doi = clean_text(paper.get("doi")).lower()
    if doi:
        return f"doi:{doi}"
    title = normalize_title(clean_text(paper.get("title")))
    authors = normalize_title(clean_text(paper.get("authors")))
    return f"title:{title}|authors:{authors}"


def _field(paper: dict[str, Any], *names: str) -> Any:
    for name in names:
        if paper.get(name) not in (None, "", []):
            return paper[name]
    return ""


def canonicalize(raw: dict[str, Any], topic: str) -> dict[str, Any]:
    title = clean_text(_field(raw, "title", "paper_title"))
    abstract = clean_text(_field(raw, "abstract", "summary", "description"))
    authors = clean_text(_field(raw, "authors", "author"))
    published = clean_text(_field(raw, "published_date", "published", "publication_date", "year"))
    paper = {
        "title": title or "Untitled paper",
        "authors": authors,
        "abstract": abstract,
        "doi": clean_text(_field(raw, "doi", "DOI")).lower(),
        "url": clean_text(_field(raw, "url", "link", "paper_url")),
        "source": clean_text(_field(raw, "source", "platform")),
        "paper_id": clean_text(_field(raw, "paper_id", "id", "identifier")),
        "published_date": published,
        "citation_count": _field(raw, "citation_count", "citations", "cited_by_count") or 0,
        "topics": [topic],
        "sources": [clean_text(_field(raw, "source", "platform"))] if _field(raw, "source", "platform") else [],
    }
    paper["key"] = paper_key(paper)
    return paper


def merge_papers(
    results: Iterable[SearchResult],
    cutoff: datetime,
    latest: datetime,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    merged: dict[str, dict[str, Any]] = {}
    rejection_counts = {"missing_date": 0, "before_cutoff": 0, "future_date": 0}
    for result in results:
        raw = result.payload
        if not isinstance(raw, dict):
            continue
        paper = canonicalize(raw, result.topic)
        published = parse_date(paper["published_date"])
        if not published:
            rejection_counts["missing_date"] += 1
            continue
        if published <= cutoff:
            rejection_counts["before_cutoff"] += 1
            continue
        if published > latest + timedelta(days=2):
            rejection_counts["future_date"] += 1
            continue
        key = paper["key"]
        existing = merged.get(key)
        if not existing:
            merged[key] = paper
            continue
        existing["topics"] = sorted(set(existing["topics"]) | set(paper["topics"]))
        existing["sources"] = sorted(set(existing["sources"]) | set(paper["sources"]))
        for field in ("abstract", "url", "doi", "paper_id", "authors", "published_date"):
            if not existing.get(field) and paper.get(field):
                existing[field] = paper[field]
    return list(merged.values()), rejection_counts


def _match_count(text: str, terms: Iterable[str]) -> int:
    lowered = text.lower()
    return sum(1 for term in terms if term.lower() in lowered)


def score_paper(paper: dict[str, Any], now: datetime) -> dict[str, Any]:
    text = " ".join((clean_text(paper.get("title")), clean_text(paper.get("abstract")))).lower()
    matched_topics: list[str] = []
    topic_hits = 0
    for topic in TOPICS:
        hits = _match_count(text, topic["terms"])
        if hits:
            matched_topics.append(topic["name"])
            topic_hits += min(hits, 3)
    topic_score = min(45, topic_hits * 7)
    published = parse_date(paper.get("published_date"))
    age_days = max(0, (now - published).days) if published else 9999
    recency_score = 20 if age_days <= 7 else 16 if age_days <= 30 else 12 if age_days <= 90 else 8 if age_days <= 365 else 0
    investor_hits = _match_count(text, INVESTOR_TERMS)
    investor_score = min(20, investor_hits * 2)
    try:
        citations = max(0, int(float(paper.get("citation_count") or 0)))
    except (TypeError, ValueError):
        citations = 0
    quality_score = min(8, int(math.log1p(citations) * 1.5))
    quality_score += 2 if paper.get("doi") else 0
    quality_score += 2 if len(clean_text(paper.get("abstract"))) >= 250 else 0
    quality_score = min(12, quality_score)
    breadth_score = min(3, max(0, len(paper.get("sources", [])) - 1))
    total = max(0, min(100, topic_score + recency_score + investor_score + quality_score + breadth_score))
    paper.update({
        "relevance_score": total,
        "score_breakdown": {
            "priority_topic": topic_score,
            "recency": recency_score,
            "investor_fit": investor_score,
            "evidence_quality": quality_score,
            "cross_source": breadth_score,
        },
        "matched_topics": matched_topics,
        "age_days": age_days,
    })
    return paper


def sort_papers(papers: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    return sorted(
        (score_paper(paper, now) for paper in papers),
        key=lambda paper: (
            -int(paper.get("relevance_score", 0)),
            -(parse_date(paper.get("published_date")) or datetime.min.replace(tzinfo=UTC)).timestamp(),
            normalize_title(paper.get("title", "")),
        ),
    )


Store = MongoStore


def run_search(config: Config, query: str) -> tuple[list[dict[str, Any]], str | None]:
    command = [config.search_command, "search", query, "--max-results", str(config.max_results_per_source), "--sources", config.search_sources]
    try:
        process = subprocess.run(command, capture_output=True, text=True, timeout=config.search_timeout_seconds, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [], str(exc)
    if process.returncode != 0:
        return [], f"paper-search exit {process.returncode}: {process.stderr[-500:]}"
    try:
        payload = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        return [], f"invalid paper-search JSON: {exc}"
    return payload.get("papers", []), None


def read_fulltext(config: Config, paper: dict[str, Any]) -> str:
    if not config.fetch_fulltext or not paper.get("paper_id") or not paper.get("source"):
        return ""
    config.download_dir.mkdir(parents=True, exist_ok=True)
    command = [config.search_command, "read", paper["source"], paper["paper_id"], "--save-path", str(config.download_dir)]
    try:
        process = subprocess.run(command, capture_output=True, text=True, timeout=config.search_timeout_seconds, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if process.returncode != 0:
        return ""
    return process.stdout[: config.max_fulltext_chars]


def cleanup_downloads(config: Config) -> None:
    if config.keep_fulltext or not config.download_dir.exists():
        return
    shutil.rmtree(config.download_dir)


def metadata_only_analysis(paper: dict[str, Any]) -> dict[str, Any]:
    abstract = clean_text(paper.get("abstract"))
    thesis = abstract.split(". ")[0].strip() if abstract else "No abstract was returned by the search source."
    return {
        "mode": "metadata-only",
        "thesis": thesis,
        "methodology": "Not assessed: configure LLM_API_KEY for a full-text/abstract analysis.",
        "surprising_result": "Not assessed without an analysis provider; no result is inferred from metadata.",
        "weaknesses": "Not assessed without an analysis provider; inspect the paper before relying on it.",
        "investor_implications": "Treat this as a reading lead, not investment advice or a validated signal.",
    }


def _strip_json_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def llm_analysis(config: Config, paper: dict[str, Any], fulltext: str) -> dict[str, Any]:
    if not config.llm_api_key:
        return metadata_only_analysis(paper)
    evidence = fulltext or clean_text(paper.get("abstract"))
    prompt = {
        "task": "Analyze this academic paper for a long-term investor who follows asset allocation, monetary economics, Bitcoin, energy, demographics, geopolitical risk, market structure, factor investing, and behavioral finance.",
        "rules": [
            "Use only the supplied metadata and text; write unknown when the evidence is missing.",
            "Separate what the paper finds from your inference for an investor.",
            "Do not provide personalized financial advice or invent numerical results.",
            "Return one JSON object with exactly these string fields: thesis, methodology, surprising_result, weaknesses, investor_implications.",
        ],
        "paper": {key: paper.get(key) for key in ("title", "authors", "published_date", "doi", "matched_topics", "relevance_score")},
        "text": evidence,
    }
    request = urllib.request.Request(
        f"{config.llm_base_url}/chat/completions",
        data=json.dumps({
            "model": config.llm_model,
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": "You are a careful academic research analyst."},
                {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
            ],
        }).encode("utf-8"),
        headers={"Authorization": f"Bearer {config.llm_api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=config.llm_timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
        content = payload["choices"][0]["message"]["content"]
        parsed = json.loads(_strip_json_fence(content))
        fields = ("thesis", "methodology", "surprising_result", "weaknesses", "investor_implications")
        if not all(isinstance(parsed.get(field), str) for field in fields):
            raise ValueError("analysis JSON is missing required string fields")
        return {"mode": "llm", **{field: parsed[field].strip() for field in fields}}
    except (OSError, urllib.error.URLError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        fallback = metadata_only_analysis(paper)
        fallback["analysis_error"] = type(exc).__name__
        return fallback


def link_for(paper: dict[str, Any]) -> str:
    return paper.get("url") or (f"https://doi.org/{paper['doi']}" if paper.get("doi") else "")


def notify_discord(config: Config, report: dict[str, Any]) -> None:
    if not config.discord_webhook_url or not report.get("success") or not report.get("paper_count"):
        return
    lines = [
        f"**Interesting papers to read: {report['paper_count']} new paper(s)**",
        f"Checked: {report.get('checked_at', 'unknown')}",
    ]
    for index, item in enumerate(report.get("top5", [])[:5], start=1):
        paper = item.get("paper", {})
        title = clean_text(paper.get("title") or "Untitled paper").replace("\n", " ")[:180]
        score = paper.get("relevance_score", 0)
        link = link_for(paper)
        lines.append(f"{index}. **{title}** — {score}/100" + (f" <{link}>" if link else ""))
    if config.paper_report_url:
        lines.append(f"Report: {config.paper_report_url}")
    content = "\n".join(lines)
    if len(content) > 1900:
        content = content[:1890].rstrip() + "…"
    request = urllib.request.Request(
        config.discord_webhook_url,
        data=json.dumps({"content": content, "allowed_mentions": {"parse": []}}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15):
            pass
        print(json.dumps({"event": "discord_notification_sent", "papers": report["paper_count"]}), flush=True)
    except (OSError, urllib.error.URLError, ValueError) as exc:
        print(json.dumps({"event": "discord_notification_failed", "error": type(exc).__name__}), flush=True)


def render_html(report: dict[str, Any]) -> str:
    def esc(value: Any) -> str:
        return html.escape(clean_text(value))

    rows = []
    for paper in report.get("papers", []):
        link = link_for(paper)
        title = f'<a href="{html.escape(link, quote=True)}" target="_blank" rel="noreferrer">{esc(paper["title"])}</a>' if link else esc(paper["title"])
        rows.append(
            "<article class=paper><div class=score>{score}<small>/100</small></div><div><h2>{title}</h2>"
            "<p class=meta>{date} · {topics} · {source}</p><p>{abstract}</p><p class=tags>{breakdown}</p></div></article>".format(
                score=paper.get("relevance_score", 0), title=title, date=esc(paper.get("published_date")),
                topics=esc(", ".join(paper.get("matched_topics", [])) or "priority match"),
                source=esc(", ".join(paper.get("sources", [])) or paper.get("source", "")),
                abstract=esc(paper.get("abstract") or "No abstract returned."),
                breakdown=esc(" · ".join(f"{key.replace('_', ' ')} {value}" for key, value in paper.get("score_breakdown", {}).items())),
            )
        )
    analyses = []
    for index, item in enumerate(report.get("top5", []), start=1):
        paper = item["paper"]
        analysis = item["analysis"]
        analyses.append(
            "<section class=analysis><h2>#{index} {title} <span>{score}/100</span></h2>"
            "<p class=meta>{topics}</p><dl><dt>Thesis</dt><dd>{thesis}</dd>"
            "<dt>Methodology</dt><dd>{methodology}</dd><dt>Surprising result</dt><dd>{surprising}</dd>"
            "<dt>Weaknesses</dt><dd>{weaknesses}</dd><dt>Investor implications</dt><dd>{implications}</dd></dl>"
            "</section>".format(
                index=index, title=esc(paper["title"]), score=paper.get("relevance_score", 0),
                topics=esc(", ".join(paper.get("matched_topics", []))), thesis=esc(analysis.get("thesis")),
                methodology=esc(analysis.get("methodology")), surprising=esc(analysis.get("surprising_result")),
                weaknesses=esc(analysis.get("weaknesses")), implications=esc(analysis.get("investor_implications")),
            )
        )
    errors = report.get("errors", {})
    error_block = f"<details><summary>Source diagnostics ({len(errors)} errors)</summary><pre>{esc(json.dumps(errors, indent=2))}</pre></details>" if errors else ""
    return """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Interesting papers to read</title>
<style>
:root{{color-scheme:light dark;--bg:#10141b;--panel:#171d27;--muted:#9ba7b7;--text:#edf2f7;--accent:#7dd3fc;--line:#2a3545}}*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font:16px/1.55 system-ui,-apple-system,Segoe UI,sans-serif}}main{{max-width:1100px;margin:0 auto;padding:32px 20px 60px}}h1{{font-size:clamp(2rem,5vw,3.5rem);line-height:1.05;margin:0 0 12px}}h2{{font-size:1.12rem;line-height:1.3;margin:0 0 6px}}a{{color:var(--accent)}}.lede,.meta{{color:var(--muted)}}.paper,.analysis{{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:18px;margin:14px 0}}.paper{{display:grid;grid-template-columns:74px 1fr;gap:16px}}.score{{font-size:2rem;font-weight:750;color:var(--accent)}}.score small{{display:block;font-size:.72rem;color:var(--muted)}}.meta{{font-size:.86rem;margin:0 0 9px}}.tags{{color:var(--muted);font-size:.82rem}}.analysis h2 span{{float:right;color:var(--accent);font-size:.9rem}}.analysis dl{{display:grid;grid-template-columns:minmax(130px,18%) 1fr;gap:8px 18px}}.analysis dt{{font-weight:700;color:var(--accent)}}.analysis dd{{margin:0}}.section-title{{margin-top:42px;border-bottom:1px solid var(--line);padding-bottom:8px}}details{{margin-top:24px;color:var(--muted)}}pre{{white-space:pre-wrap}}footer{{color:var(--muted);font-size:.82rem;margin-top:32px}}@media(max-width:650px){{.paper{{grid-template-columns:1fr}}.analysis dl{{display:block}}.analysis dt{{margin-top:12px}}}}
</style></head><body><main><h1>Interesting papers to read</h1><p class=lede>New since {cutoff}: {count} papers · checked {checked} · ranking is a reproducible relevance heuristic, not investment advice.</p>
<h2 class=section-title>New discoveries</h2>{papers}<h2 class=section-title>Top five analysis</h2>{analyses}{errors}<footer>Analysis mode: {mode}. Sources and paper links are retained in the JSON report beside this page.</footer></main></body></html>""".format(
        cutoff=esc(report.get("cutoff_at")), count=report.get("paper_count", 0), checked=esc(report.get("checked_at")),
        papers="".join(rows) or "<p class=lede>No new dated papers passed the cutoff on this run.</p>",
        analyses="".join(analyses) or "<p class=lede>No top-five analysis was generated.</p>", errors=error_block,
        mode=esc(report.get("analysis_mode", "metadata-only")),
    )


REPORT_NAMES = ("latest.json", "latest.html")


def write_reports(config: Config, report: dict[str, Any]) -> None:
    config.report_dir.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    page = render_html(report)
    for name, content in zip(REPORT_NAMES, (payload, page)):
        temporary = config.report_dir / f".{name}.tmp"
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, config.report_dir / name)


def clear_reports(config: Config) -> None:
    for name in REPORT_NAMES:
        (config.report_dir / name).unlink(missing_ok=True)
        (config.report_dir / f".{name}.tmp").unlink(missing_ok=True)


def reset_and_run(config: Config) -> dict[str, Any]:
    clear_reports(config)
    store = Store(config)
    try:
        counts = store.reset()
        print(json.dumps({"event": "reset_completed", **counts}), flush=True)
        return _run_once(config, store)
    finally:
        store.close()


def run_once(config: Config) -> dict[str, Any]:
    store = Store(config)
    try:
        return _run_once(config, store)
    finally:
        store.close()


def _run_once(config: Config, store: MongoStore) -> dict[str, Any]:
    started_at = now_utc()
    print(json.dumps({"event": "run_started", "at": iso(started_at), "sources": config.search_sources}), flush=True)
    last_success = parse_date(store.get_state("last_success_at"))
    cutoff = last_success or (started_at - timedelta(days=config.lookback_days))
    search_results: list[SearchResult] = []
    errors: dict[str, str] = {}
    for topic in TOPICS:
        papers, error = run_search(config, topic["query"])
        if error:
            errors[topic["name"]] = error
        search_results.extend(SearchResult(topic["name"], paper) for paper in papers)
        print(json.dumps({"event": "topic_search", "topic": topic["name"], "results": len(papers), "ok": error is None}), flush=True)
    papers, rejection_counts = merge_papers(search_results, cutoff, started_at)
    print(json.dumps({"event": "filter_summary", "cutoff": iso(cutoff), "accepted": len(papers), **rejection_counts}), flush=True)
    papers = sort_papers(papers, started_at)
    for paper in papers:
        paper["fulltext_available"] = False
    top5 = []
    for paper in papers[:5]:
        fulltext = read_fulltext(config, paper)
        paper["fulltext_available"] = bool(fulltext)
        analysis = llm_analysis(config, paper, fulltext)
        paper["analysis"] = analysis
        top5.append({"paper": paper, "analysis": analysis})
    cleanup_downloads(config)
    success = len(errors) == 0
    finished_at = now_utc()
    if papers:
        store.save_papers(papers, iso(finished_at))
    if success:
        store.set_state("last_success_at", iso(finished_at))
    report = {
        "checked_at": iso(finished_at),
        "cutoff_at": iso(cutoff),
        "paper_count": len(papers),
        "excluded_missing_date": rejection_counts["missing_date"],
        "excluded_before_cutoff": rejection_counts["before_cutoff"],
        "excluded_future_date": rejection_counts["future_date"],
        "success": success,
        "errors": errors,
        "papers": papers,
        "top5": top5,
        "analysis_mode": "llm" if config.llm_api_key else "metadata-only",
        "priorities": [topic["name"] for topic in TOPICS],
        "retrieval": {"sources": config.search_sources.split(","), "max_results_per_source": config.max_results_per_source},
    }
    write_reports(config, report)
    store.save_run(iso(started_at), iso(finished_at), iso(cutoff), success, len(papers), errors)
    notify_discord(config, report)
    print(json.dumps({"event": "run_finished", "at": iso(finished_at), "papers": len(papers), "success": success}), flush=True)
    return report


def healthcheck(config: Config) -> int:
    try:
        store = Store(config)
        store.close()
    except (OSError, RuntimeError):
        return 1
    return 0


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--healthcheck", action="store_true")
    parser.add_argument("--reset-and-run", action="store_true")
    parser.add_argument("--confirm-reset", action="store_true")
    parser.add_argument("--lookback-days", type=positive_int)
    args = parser.parse_args()
    if args.reset_and_run and not args.confirm_reset:
        parser.error("--reset-and-run requires --confirm-reset")
    if args.reset_and_run and (args.loop or args.healthcheck or args.once):
        parser.error("--reset-and-run cannot be combined with --once, --loop, or --healthcheck")
    config = Config.from_env()
    if args.lookback_days is not None:
        config = replace(config, lookback_days=args.lookback_days)
    if args.reset_and_run:
        report = reset_and_run(config)
    elif args.healthcheck:
        return healthcheck(config)
    elif args.once or not args.loop:
        report = run_once(config)
    else:
        if config.run_on_startup:
            run_once(config)
        while True:
            time.sleep(config.check_interval_hours * 3600)
            run_once(config)
    print(json.dumps({"success": report["success"], "paper_count": report["paper_count"], "errors": report["errors"]}))
    return 0 if report["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
