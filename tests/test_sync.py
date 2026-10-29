"""Tests for sync.py: state parsing, dedupe, enrichment helpers, output schema."""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sync  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "state_sample.json")


def load_fixture():
    with open(FIXTURE, encoding="utf-8") as handle:
        return json.load(handle)


# --------------------------------------------------------------------------- #
# State parsing
# --------------------------------------------------------------------------- #


class ParseStateTests(unittest.TestCase):
    def test_parses_valid_state(self):
        parsed = sync.parse_state(load_fixture())
        self.assertEqual(len(parsed["records"]), 5)
        self.assertEqual(parsed["summaries"]["2026-01-03"], "day three summary")
        self.assertEqual(parsed["preferences"]["themes"], ["self-hosted"])

    def test_rejects_non_object(self):
        with self.assertRaises(ValueError):
            sync.parse_state(["not", "a", "dict"])

    def test_rejects_missing_projects(self):
        with self.assertRaises(ValueError):
            sync.parse_state({"version": 1})

    def test_rejects_record_without_slash(self):
        bad = {"recommended_projects": [{"date": "2026-01-01", "full_name": "noslash"}]}
        with self.assertRaises(ValueError):
            sync.parse_state(bad)

    def test_rejects_record_without_date(self):
        bad = {"recommended_projects": [{"full_name": "a/b"}]}
        with self.assertRaises(ValueError):
            sync.parse_state(bad)

    def test_missing_tags_default_to_empty(self):
        ok = {"recommended_projects": [{"date": "2026-01-01", "full_name": "a/b"}]}
        parsed = sync.parse_state(ok)
        self.assertEqual(parsed["records"][0]["reason_tags"], [])
        self.assertEqual(
            parsed["records"][0]["url"], "https://github.com/a/b"
        )


# --------------------------------------------------------------------------- #
# Dedupe
# --------------------------------------------------------------------------- #


class DedupeTests(unittest.TestCase):
    def setUp(self):
        self.records = sync.parse_state(load_fixture())["records"]

    def test_collapses_duplicates(self):
        projects = sync.dedupe_projects(self.records)
        self.assertEqual(len(projects), 3)  # alpha/one, beta/two, gamma/three

    def test_preserves_recommendation_history(self):
        projects = sync.dedupe_projects(self.records)
        self.assertEqual(
            projects["alpha/one"]["recommendation_dates"], ["2026-01-02", "2026-01-03"]
        )
        self.assertEqual(
            projects["beta/two"]["recommendation_dates"], ["2026-01-01", "2026-01-03"]
        )

    def test_tag_union_ordered_unique(self):
        projects = sync.dedupe_projects(self.records)
        # alpha/one: first seen 2026-01-03 record -> ["self-hosted","mcp"], then "sqlite"
        self.assertEqual(
            projects["alpha/one"]["reason_tags"], ["self-hosted", "mcp", "sqlite"]
        )
        self.assertEqual(
            projects["beta/two"]["reason_tags"], ["local-first", "observability"]
        )

    def test_preserves_first_seen_order(self):
        projects = sync.dedupe_projects(self.records)
        self.assertEqual(list(projects.keys()), ["alpha/one", "beta/two", "gamma/three"])

    def test_dedupes_github_names_case_insensitively(self):
        records = [
            {"full_name": "Agenta-AI/agenta", "url": "https://github.com/Agenta-AI/agenta", "date": "2026-01-01", "reason_tags": ["first"]},
            {"full_name": "agenta-ai/agenta", "url": "https://github.com/agenta-ai/agenta", "date": "2026-01-02", "reason_tags": ["second"]},
        ]
        projects = sync.dedupe_projects(records)
        self.assertEqual(list(projects), ["Agenta-AI/agenta"])
        self.assertEqual(projects["Agenta-AI/agenta"]["recommendation_dates"], ["2026-01-01", "2026-01-02"])
        self.assertEqual(projects["Agenta-AI/agenta"]["reason_tags"], ["first", "second"])


# --------------------------------------------------------------------------- #
# Days
# --------------------------------------------------------------------------- #


class BuildDaysTests(unittest.TestCase):
    def test_groups_descending_with_unique_names(self):
        records = sync.parse_state(load_fixture())["records"]
        days = sync.build_days(records, {"2026-01-03": "s3"})
        self.assertEqual([d["date"] for d in days], ["2026-01-03", "2026-01-02", "2026-01-01"])
        self.assertEqual(days[0]["projects"], ["alpha/one", "beta/two"])
        self.assertEqual(days[0]["preference_summary"], "s3")
        self.assertEqual(days[1]["projects"], ["alpha/one", "gamma/three"])

    def test_day_references_use_first_seen_case(self):
        records = [
            {"full_name": "NevaMind-AI/MemU", "date": "2026-01-01"},
            {"full_name": "NevaMind-AI/memU", "date": "2026-01-02"},
        ]
        days = sync.build_days(records, {})
        self.assertEqual(days[0]["projects"], ["NevaMind-AI/MemU"])
        self.assertEqual(days[1]["projects"], ["NevaMind-AI/MemU"])


# --------------------------------------------------------------------------- #
# GraphQL query building + node normalisation
# --------------------------------------------------------------------------- #


class GraphQLTests(unittest.TestCase):
    def test_query_contains_all_aliases(self):
        query = sync.build_graphql_query(["a/b", "c/d", "e/f"])
        self.assertIn('repo0: repository(owner: "a", name: "b")', query)
        self.assertIn('repo2: repository(owner: "e", name: "f")', query)
        self.assertTrue(query.strip().startswith("query BatchEnrich"))
        self.assertIn("stargazerCount", query)

    def test_query_escapes_quotes(self):
        query = sync.build_graphql_query(['we"ird/x'])
        self.assertIn('owner: "we\\"ird"', query)

    def test_normalize_node(self):
        node = {
            "nameWithOwner": "a/b",
            "name": "b",
            "url": "https://github.com/a/b",
            "description": None,
            "homepageUrl": "",
            "stargazerCount": 42,
            "forkCount": 3,
            "watchers": {"totalCount": 5},
            "primaryLanguage": {"name": "Go", "color": "#00ADD8"},
            "licenseInfo": {"spdxId": "MIT", "name": "MIT License"},
            "isArchived": False,
            "isFork": False,
            "repositoryTopics": {"nodes": [{"topic": {"name": "cli"}}]},
            "issues": {"totalCount": 7},
            "pullRequests": {"totalCount": 1},
            "createdAt": "2026-01-01T00:00:00Z",
            "updatedAt": "2026-01-02T00:00:00Z",
            "pushedAt": "2026-01-03T00:00:00Z",
            "defaultBranchRef": {"name": "main"},
        }
        out = sync.normalize_repo_node(node)
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["description"], "")
        self.assertEqual(out["language"], "Go")
        self.assertEqual(out["license"], "MIT")
        self.assertEqual(out["topics"], ["cli"])
        self.assertEqual(out["stars"], 42)
        self.assertEqual(out["open_issues"], 7)

    def test_normalize_node_handles_nulls(self):
        out = sync.normalize_repo_node({"stargazerCount": 0})
        self.assertEqual(out["status"], "ok")
        self.assertIsNone(out["language"])
        self.assertEqual(out["topics"], [])


# --------------------------------------------------------------------------- #
# Dataset schema
# --------------------------------------------------------------------------- #


class BuildDatasetTests(unittest.TestCase):
    def _dataset(self):
        state = load_fixture()
        parsed = sync.parse_state(state)
        projects = sync.dedupe_projects(parsed["records"])
        days = sync.build_days(parsed["records"], parsed["summaries"])
        metadata = {
            "alpha/one": {
                "status": "ok", "description": "d1", "homepage": "", "stars": 100,
                "forks": 5, "watchers": 2, "language": "Go", "language_color": "#00ADD8",
                "license": "MIT", "topics": ["cli"], "archived": False, "fork": False,
                "open_issues": 1, "open_pulls": 0, "default_branch": "main",
                "created_at": "2026-01-01", "updated_at": "2026-01-02",
                "pushed_at": "2026-01-03", "fetched_at": "2026-01-04",
            },
            "beta/two": {"status": "not_found"},
            "gamma/three": {"status": "error"},
        }
        return sync.build_dataset(
            state, metadata, parsed["records"], projects, days, parsed["preferences"]
        )

    def test_top_level_schema(self):
        d = self._dataset()
        for key in ("generated_at", "stats", "days", "projects", "preferences"):
            self.assertIn(key, d)
        self.assertIsInstance(d["projects"], list)
        self.assertIsInstance(d["days"], list)

    def test_project_schema(self):
        d = self._dataset()
        p = next(x for x in d["projects"] if x["full_name"] == "alpha/one")
        for key in (
            "full_name", "owner", "name", "url", "reason_tags",
            "recommendation_dates", "recommendation_count",
            "first_recommended", "last_recommended", "github",
        ):
            self.assertIn(key, p)
        self.assertEqual(p["owner"], "alpha")
        self.assertEqual(p["name"], "one")
        self.assertEqual(p["recommendation_count"], 2)
        self.assertEqual(p["github"]["stars"], 100)
        self.assertEqual(p["github"]["status"], "ok")

    def test_missing_repo_is_not_found_not_crash(self):
        d = self._dataset()
        p = next(x for x in d["projects"] if x["full_name"] == "beta/two")
        self.assertEqual(p["github"]["status"], "not_found")
        self.assertIsNone(p["github"]["stars"])

    def test_stats_aggregate(self):
        d = self._dataset()
        s = d["stats"]
        self.assertEqual(s["total_records"], 5)
        self.assertEqual(s["unique_projects"], 3)
        self.assertEqual(s["total_days"], 3)
        self.assertEqual(s["enriched_ok"], 1)
        self.assertEqual(s["enriched_total"], 3)
        self.assertEqual(s["languages"][0]["name"], "Go")
        self.assertIn("self-hosted", s["all_tags"])

    def test_projects_sorted_by_recency(self):
        d = self._dataset()
        dates = [p["last_recommended"] for p in d["projects"]]
        self.assertEqual(dates, sorted(dates, reverse=True))


# --------------------------------------------------------------------------- #
# Atomic write + end-to-end offline sync
# --------------------------------------------------------------------------- #


class SyncIntegrationTests(unittest.TestCase):
    def test_atomic_write_and_offline_sync(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "projects.json")
            cache = os.path.join(tmp, "cache.json")
            dataset = sync.sync(
                state_path=FIXTURE,
                output_path=out,
                cache_path=cache,
                offline=True,
                log=lambda *a, **k: None,
            )
            self.assertTrue(os.path.exists(out))
            self.assertTrue(os.path.exists(cache))
            with open(out, encoding="utf-8") as handle:
                on_disk = json.load(handle)
            self.assertEqual(on_disk["stats"]["total_records"], 5)
            # No stray temp files left behind.
            leftovers = [f for f in os.listdir(tmp) if f.startswith(".tmp-")]
            self.assertEqual(leftovers, [])
            self.assertEqual(dataset["stats"]["unique_projects"], 3)

    def test_atomic_write_creates_parent_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "nested", "deep", "x.json")
            sync.atomic_write_json(target, {"ok": True})
            self.assertTrue(os.path.exists(target))


if __name__ == "__main__":
    unittest.main()
