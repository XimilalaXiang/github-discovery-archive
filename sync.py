#!/usr/bin/env python3
"""Sync GitHub daily recommendations state -> static site data.

Reads the cron state file (source of truth), dedupes recommendation records by
repository ``full_name`` while preserving the full recommendation history, then
enriches each unique repository with GitHub metadata fetched in batched GraphQL
queries (via the ``gh`` CLI). Metadata is cached locally so repeated runs are
cheap, and the final dataset is written atomically to ``data/projects.json``.

Design rules:
  * Only stdlib + the ``gh`` CLI. No third-party Python packages.
  * A repository that no longer exists (or is inaccessible) must never break the
    run: it is recorded with ``github.status == "not_found"``.
  * Output is deterministic (stable ordering) so diffs are meaningful.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter, OrderedDict
from datetime import datetime, timezone
from typing import Any, Iterable

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_STATE_PATH = os.path.expanduser(
    "~/.hermes/cron/state/github_daily_recs_state.json"
)
DEFAULT_OUTPUT_PATH = os.path.join(PROJECT_ROOT, "data", "projects.json")
DEFAULT_CACHE_PATH = os.path.join(PROJECT_ROOT, "data", "metadata_cache.json")

# How many repositories to request per GraphQL round-trip.
BATCH_SIZE = 25

# Default cache freshness window (seconds). 24h matches the daily cron cadence.
DEFAULT_CACHE_TTL = 24 * 60 * 60


# --------------------------------------------------------------------------- #
# Utilities
# --------------------------------------------------------------------------- #


def now_iso() -> str:
    """Current UTC time as an ISO-8601 string with a trailing ``Z``."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def atomic_write_json(path: str, payload: Any) -> None:
    """Write ``payload`` as JSON to ``path`` atomically (temp file + rename)."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def read_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


# --------------------------------------------------------------------------- #
# State parsing + dedupe
# --------------------------------------------------------------------------- #


def parse_state(state: dict) -> dict:
    """Validate + normalise the raw cron state into a canonical structure.

    Returns a dict with keys ``records``, ``days`` and ``preferences``.
    Raises ``ValueError`` on structurally invalid input.
    """
    if not isinstance(state, dict):
        raise ValueError("state root must be a JSON object")

    records_raw = state.get("recommended_projects")
    if not isinstance(records_raw, list):
        raise ValueError("state missing 'recommended_projects' list")

    records: list[dict] = []
    for idx, item in enumerate(records_raw):
        if not isinstance(item, dict):
            raise ValueError(f"record #{idx} is not an object")
        full_name = (item.get("full_name") or "").strip()
        date = (item.get("date") or "").strip()
        if not full_name or "/" not in full_name:
            raise ValueError(f"record #{idx} has invalid full_name: {full_name!r}")
        if not date:
            raise ValueError(f"record #{idx} ({full_name}) has no date")
        tags = item.get("reason_tags") or []
        if not isinstance(tags, list):
            tags = []
        records.append(
            {
                "date": date,
                "full_name": full_name,
                "url": item.get("url") or f"https://github.com/{full_name}",
                "reason_tags": [str(t) for t in tags],
            }
        )

    # delivery_history contributes per-day preference summaries.
    delivery = state.get("delivery_history") or []
    summaries: dict[str, str] = {}
    if isinstance(delivery, list):
        for entry in delivery:
            if isinstance(entry, dict) and entry.get("date"):
                summary = entry.get("preference_summary")
                if summary:
                    summaries[entry["date"]] = str(summary)

    preferences = state.get("inferred_preferences") or {}

    return {"records": records, "summaries": summaries, "preferences": preferences}


def dedupe_projects(records: Iterable[dict]) -> "OrderedDict[str, dict]":
    """Collapse records by case-insensitive ``full_name``.

    GitHub owner and repository names are case-insensitive.  The state contains
    historical casing changes (for example ``Agenta-AI`` vs ``agenta-ai``), so
    exact-string dedupe would create duplicate cards and broken day references.
    The first-seen spelling remains the canonical display name.

    Output preserves first-seen order. Each project keeps the ordered union of
    its ``reason_tags`` and the sorted list of every date it was recommended.
    """
    projects: "OrderedDict[str, dict]" = OrderedDict()
    canonical_by_lower: dict[str, str] = {}
    for record in records:
        raw_fn = record["full_name"]
        key = raw_fn.casefold()
        fn = canonical_by_lower.get(key)
        if fn is None:
            fn = raw_fn
            canonical_by_lower[key] = fn
        project = projects.get(fn)
        if project is None:
            project = {
                "full_name": fn,
                "url": record["url"],
                "reason_tags": [],
                "recommendation_dates": [],
            }
            projects[fn] = project
        # Ordered tag union (no duplicates, first-seen order wins).
        for tag in record["reason_tags"]:
            if tag not in project["reason_tags"]:
                project["reason_tags"].append(tag)
        project["recommendation_dates"].append(record["date"])
    for project in projects.values():
        project["recommendation_dates"] = sorted(project["recommendation_dates"])
    return projects


def build_days(records: Iterable[dict], summaries: dict[str, str]) -> list[dict]:
    """Group records by date (descending) with ordered full_names + summary."""
    by_date: "OrderedDict[str, list[str]]" = OrderedDict()
    canonical_by_lower: dict[str, str] = {}
    for record in records:
        raw_fn = record["full_name"]
        fn = canonical_by_lower.setdefault(raw_fn.casefold(), raw_fn)
        by_date.setdefault(record["date"], []).append(fn)
    days = []
    for date in sorted(by_date.keys(), reverse=True):
        names = by_date[date]
        # De-duplicate names within a day while keeping order.
        seen: list[str] = []
        for n in names:
            if n not in seen:
                seen.append(n)
        days.append(
            {
                "date": date,
                "projects": seen,
                "count": len(seen),
                "preference_summary": summaries.get(date, ""),
            }
        )
    return days


# --------------------------------------------------------------------------- #
# GitHub metadata enrichment
# --------------------------------------------------------------------------- #

_REPO_FIELDS = """
  nameWithOwner
  name
  url
  description
  homepageUrl
  stargazerCount
  forkCount
  watchers { totalCount }
  primaryLanguage { name color }
  licenseInfo { spdxId name }
  isArchived
  isFork
  isDisabled
  isTemplate
  createdAt
  updatedAt
  pushedAt
  repositoryTopics(first: 20) { nodes { topic { name } } }
  issues(states: OPEN) { totalCount }
  pullRequests(states: OPEN) { totalCount }
  defaultBranchRef { name }
"""


def build_graphql_query(full_names: list[str]) -> str:
    """Build a batched GraphQL query with one aliased ``repository`` per repo."""
    parts = ["query BatchEnrich {"]
    for idx, fn in enumerate(full_names):
        owner, _, name = fn.partition("/")
        # GraphQL string values are double-quoted; escape defensively.
        owner = owner.replace('"', '\\"')
        name = name.replace('"', '\\"')
        parts.append(
            f'  repo{idx}: repository(owner: "{owner}", name: "{name}") '
            f"{{{_REPO_FIELDS}  }}"
        )
    parts.append("}")
    return "\n".join(parts)


def normalize_repo_node(node: dict) -> dict:
    """Flatten a GraphQL repository node into the site metadata shape."""
    lang = node.get("primaryLanguage") or {}
    license_info = node.get("licenseInfo") or {}
    topics = [
        t["topic"]["name"]
        for t in (node.get("repositoryTopics") or {}).get("nodes", [])
        if t and t.get("topic")
    ]
    watchers = (node.get("watchers") or {}).get("totalCount")
    return {
        "status": "ok",
        "name_with_owner": node.get("nameWithOwner"),
        "name": node.get("name"),
        "url": node.get("url"),
        "description": node.get("description") or "",
        "homepage": node.get("homepageUrl") or "",
        "stars": node.get("stargazerCount"),
        "forks": node.get("forkCount"),
        "watchers": watchers,
        "language": lang.get("name"),
        "language_color": lang.get("color"),
        "license": license_info.get("spdxId") or license_info.get("name"),
        "topics": topics,
        "archived": bool(node.get("isArchived")),
        "fork": bool(node.get("isFork")),
        "disabled": bool(node.get("isDisabled")),
        "template": bool(node.get("isTemplate")),
        "open_issues": (node.get("issues") or {}).get("totalCount"),
        "open_pulls": (node.get("pullRequests") or {}).get("totalCount"),
        "default_branch": (node.get("defaultBranchRef") or {}).get("name"),
        "created_at": node.get("createdAt"),
        "updated_at": node.get("updatedAt"),
        "pushed_at": node.get("pushedAt"),
    }


def run_gh_graphql(query: str, timeout: int = 120) -> tuple[dict | None, list, str | None]:
    """Execute a GraphQL query via ``gh``.

    Returns ``(data, errors, transport_error)`` where:
      * ``data``            is the GraphQL ``data`` object (or ``None``),
      * ``errors``          is the GraphQL ``errors`` list (possibly empty),
      * ``transport_error`` is a non-None string for spawn/parse/timeout issues.

    ``gh`` exits non-zero when the response contains GraphQL ``errors`` (e.g. a
    missing repo) while still printing the JSON body; we therefore parse stdout
    regardless of the exit code.
    """
    if shutil.which("gh") is None:
        return None, [], "gh CLI not found on PATH"
    try:
        proc = subprocess.run(
            ["gh", "api", "graphql", "-f", f"query={query}"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None, [], "gh api graphql timed out"
    except OSError as exc:  # pragma: no cover - environment dependent
        return None, [], f"failed to run gh: {exc}"

    stdout = (proc.stdout or "").strip()
    if not stdout:
        return None, [], (proc.stderr or "empty response from gh").strip()
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        return None, [], f"invalid JSON from gh: {exc}"

    data = payload.get("data")
    errors = payload.get("errors") or []
    return data, errors, None


def fetch_batch(full_names: list[str], retries: int = 3, backoff: float = 1.5) -> dict[str, dict]:
    """Fetch metadata for a batch of repos. Returns {full_name: metadata|None}.

    Retries transient failures (network hiccups, secondary rate limits) with
    exponential backoff before giving up.
    """
    query = build_graphql_query(full_names)
    data = None
    errors: list = []
    transport_error = None
    for attempt in range(retries):
        data, errors, transport_error = run_gh_graphql(query)
        if data is not None:
            break
        if attempt < retries - 1:
            time.sleep(backoff * (2**attempt))

    result: dict[str, dict] = {}
    if data is None:
        return result  # caller decides how to handle total failure
    for idx, fn in enumerate(full_names):
        node = data.get(f"repo{idx}")
        if node is None:
            result[fn] = {"status": "not_found"}
        else:
            result[fn] = normalize_repo_node(node)
    return result


def enrich_metadata(
    full_names: list[str],
    cache: dict[str, dict],
    cache_ttl: int,
    refresh: bool = False,
    offline: bool = False,
    log=print,
) -> tuple[dict[str, dict], dict[str, dict]]:
    """Populate metadata for every repo, using + updating the local cache.

    Returns ``(metadata_by_name, new_cache)``.
    """
    metadata: dict[str, dict] = {}
    new_cache: dict[str, dict] = dict(cache)
    now = time.time()

    stale: list[str] = []
    for fn in full_names:
        cached = cache.get(fn)
        # Only a successful/known result may satisfy the cache. Transport
        # failures (status == "error") are always retried.
        if (
            not refresh
            and isinstance(cached, dict)
            and cached.get("status") in ("ok", "not_found")
            and cached.get("fetched_at_ts")
            and (now - float(cached["fetched_at_ts"])) < cache_ttl
        ):
            metadata[fn] = cached
        else:
            stale.append(fn)

    if offline:
        for fn in stale:
            cached = cache.get(fn)
            metadata[fn] = cached if isinstance(cached, dict) else {"status": "unknown"}
        log(f"[sync] offline mode: {len(stale)} repo(s) missing fresh metadata")
        return metadata, new_cache

    log(
        f"[sync] {len(full_names)} unique repos; "
        f"{len(full_names) - len(stale)} cached, {len(stale)} to fetch"
    )

    fetched_at = now_iso()
    for start in range(0, len(stale), BATCH_SIZE):
        batch = stale[start : start + BATCH_SIZE]
        log(
            f"[sync] fetching batch {start // BATCH_SIZE + 1}"
            f"/{(len(stale) + BATCH_SIZE - 1) // BATCH_SIZE} ({len(batch)} repos)"
        )
        try:
            batch_result = fetch_batch(batch)
        except Exception as exc:  # pragma: no cover - defensive
            log(f"[sync] batch failed: {exc}")
            batch_result = {}

        for fn in batch:
            node = batch_result.get(fn)
            if node is None:
                # Whole-batch failure: keep previous cache if any.
                prev = cache.get(fn)
                node = prev if isinstance(prev, dict) else {"status": "error"}
                if node.get("status") == "ok":
                    node = dict(node)  # don't mutate cache dict
            entry = dict(node)
            entry["fetched_at"] = fetched_at
            entry["fetched_at_ts"] = now
            metadata[fn] = entry
            # Persist successful / not_found results; drop hard failures so the
            # next run retries them instead of caching a dead entry.
            if entry.get("status") in ("ok", "not_found"):
                new_cache[fn] = entry
            else:
                new_cache.pop(fn, None)

    return metadata, new_cache


# --------------------------------------------------------------------------- #
# Dataset assembly
# --------------------------------------------------------------------------- #


def build_dataset(
    state: dict,
    metadata: dict[str, dict],
    records: list[dict],
    projects: "OrderedDict[str, dict]",
    days: list[dict],
    preferences: dict,
) -> dict:
    """Assemble the final ``projects.json`` payload."""
    enriched_ok = sum(1 for m in metadata.values() if m.get("status") == "ok")

    # Aggregate stats.
    language_counter: Counter = Counter()
    topic_counter: Counter = Counter()
    tag_counter: Counter = Counter()
    owner_counter: Counter = Counter()
    stars_total = 0
    stars_known = 0

    out_projects = []
    for fn, project in projects.items():
        meta = metadata.get(fn, {"status": "unknown"})
        gh = {
            "status": meta.get("status", "unknown"),
            "description": meta.get("description", ""),
            "homepage": meta.get("homepage", ""),
            "stars": meta.get("stars"),
            "forks": meta.get("forks"),
            "watchers": meta.get("watchers"),
            "language": meta.get("language"),
            "language_color": meta.get("language_color"),
            "license": meta.get("license"),
            "topics": meta.get("topics", []),
            "archived": meta.get("archived", False),
            "fork": meta.get("fork", False),
            "open_issues": meta.get("open_issues"),
            "open_pulls": meta.get("open_pulls"),
            "default_branch": meta.get("default_branch"),
            "created_at": meta.get("created_at"),
            "updated_at": meta.get("updated_at"),
            "pushed_at": meta.get("pushed_at"),
            "fetched_at": meta.get("fetched_at"),
        }
        owner = fn.split("/", 1)[0]
        item = {
            "full_name": fn,
            "owner": owner,
            "name": fn.split("/", 1)[1] if "/" in fn else fn,
            "url": project["url"],
            "reason_tags": project["reason_tags"],
            "recommendation_dates": project["recommendation_dates"],
            "recommendation_count": len(project["recommendation_dates"]),
            "first_recommended": project["recommendation_dates"][0],
            "last_recommended": project["recommendation_dates"][-1],
            "github": gh,
        }
        out_projects.append(item)

        for tag in project["reason_tags"]:
            tag_counter[tag] += 1
        owner_counter[owner] += 1
        if gh["status"] == "ok":
            if gh["language"]:
                language_counter[gh["language"]] += 1
            for topic in gh["topics"]:
                topic_counter[topic] += 1
            if isinstance(gh["stars"], int):
                stars_total += gh["stars"]
                stars_known += 1

    # Stable sort: most recent recommendation first, then stars, then name.
    def sort_key(p: dict):
        stars = p["github"].get("stars")
        return (
            p["last_recommended"],
            stars if isinstance(stars, int) else -1,
            p["full_name"],
        )

    out_projects.sort(key=sort_key, reverse=True)

    all_tags = sorted(tag_counter.keys())
    all_languages = sorted(language_counter.keys())

    stats = {
        "total_records": len(records),
        "unique_projects": len(projects),
        "total_days": len(days),
        "date_min": days[-1]["date"] if days else None,
        "date_max": days[0]["date"] if days else None,
        "enriched_ok": enriched_ok,
        "enriched_total": len(projects),
        "stars_total": stars_total,
        "stars_known": stars_known,
        "languages": [
            {"name": name, "count": count}
            for name, count in language_counter.most_common()
        ],
        "tags": [
            {"name": name, "count": count} for name, count in tag_counter.most_common()
        ],
        "topics": [
            {"name": name, "count": count} for name, count in topic_counter.most_common(40)
        ],
        "top_owners": [
            {"name": name, "count": count} for name, count in owner_counter.most_common(20)
        ],
        "all_tags": all_tags,
        "all_languages": all_languages,
    }

    return {
        "generated_at": now_iso(),
        "source_version": state.get("version"),
        "source_created_at": state.get("created_at"),
        "preferences": preferences,
        "stats": stats,
        "days": days,
        "projects": out_projects,
    }


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def sync(
    state_path: str,
    output_path: str,
    cache_path: str,
    cache_ttl: int = DEFAULT_CACHE_TTL,
    refresh: bool = False,
    offline: bool = False,
    log=print,
) -> dict:
    """Run a full sync. Returns the assembled dataset (also written to disk)."""
    log(f"[sync] reading state: {state_path}")
    state = read_json(state_path)
    parsed = parse_state(state)
    records = parsed["records"]
    log(f"[sync] {len(records)} recommendation records")

    projects = dedupe_projects(records)
    log(f"[sync] {len(projects)} unique projects after dedupe")

    days = build_days(records, parsed["summaries"])

    cache: dict = {}
    if os.path.exists(cache_path):
        try:
            loaded = read_json(cache_path)
            if isinstance(loaded, dict):
                cache = loaded.get("repos", loaded)
                if not isinstance(cache, dict):
                    cache = {}
        except (OSError, json.JSONDecodeError) as exc:
            log(f"[sync] ignoring unreadable cache ({exc})")
            cache = {}

    metadata, new_cache = enrich_metadata(
        list(projects.keys()),
        cache=cache,
        cache_ttl=cache_ttl,
        refresh=refresh,
        offline=offline,
        log=log,
    )

    # Remove cache entries that are no longer referenced (keeps file small).
    referenced = set(projects.keys())
    new_cache = {k: v for k, v in new_cache.items() if k in referenced}

    atomic_write_json(cache_path, {"updated_at": now_iso(), "repos": new_cache})
    log(f"[sync] wrote cache: {cache_path}")

    dataset = build_dataset(
        state=state,
        metadata=metadata,
        records=records,
        projects=projects,
        days=days,
        preferences=parsed["preferences"],
    )
    atomic_write_json(output_path, dataset)
    log(
        f"[sync] wrote dataset: {output_path} "
        f"({dataset['stats']['unique_projects']} projects, "
        f"{dataset['stats']['enriched_ok']} enriched ok)"
    )
    return dataset


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", default=DEFAULT_STATE_PATH, help="cron state file")
    parser.add_argument("--output", default=DEFAULT_OUTPUT_PATH, help="output json")
    parser.add_argument("--cache", default=DEFAULT_CACHE_PATH, help="metadata cache")
    parser.add_argument(
        "--cache-ttl",
        type=int,
        default=DEFAULT_CACHE_TTL,
        help="cache freshness window in seconds",
    )
    parser.add_argument(
        "--refresh", action="store_true", help="ignore cache, refetch all metadata"
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="do not call GitHub; use cache only (missing entries marked unknown)",
    )
    parser.add_argument("--quiet", action="store_true", help="suppress progress logs")
    args = parser.parse_args(argv)

    log = (lambda *a, **k: None) if args.quiet else print
    try:
        sync(
            state_path=args.state,
            output_path=args.output,
            cache_path=args.cache,
            cache_ttl=args.cache_ttl,
            refresh=args.refresh,
            offline=args.offline,
            log=log,
        )
    except (OSError, ValueError) as exc:
        print(f"[sync] error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
