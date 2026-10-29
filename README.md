# GitHub 每日发现档案 · GitHub Discovery Archive

A polished, dark-native (Linear/Vercel-inspired) dynamic archive of the *daily
GitHub repository recommendations* — a searchable timeline, gallery and stats
view over every day's picks, in Chinese-first UI.

**Live site:** <https://projects.ximilala.com>

- **Source of truth:** `~/.hermes/cron/state/github_daily_recs_state.json`
  (written by the daily recommendation cron; keeps growing).
- **Data pipeline:** `sync.py` reads the state, dedupes by `full_name`
  (preserving the full recommendation history), enriches every repo with GitHub
  metadata via batched GraphQL (`gh api graphql`), caches metadata locally, and
  writes `data/projects.json` **atomically**.
- **Site:** static `web/` (vanilla HTML/CSS/JS, no framework) served by a
  pure-stdlib Python server (`serve.py`).
- **Public & clean:** only publicly recommended repositories — no private user
  repos, no secrets in the client bundle.

---

## Quick start

```bash
cd /home/ubuntu/github-discovery-archive

# 1. Build/refresh the dataset (needs an authenticated `gh`)
python3 sync.py

# 2. Serve the site
python3 serve.py --host 127.0.0.1 --port 8793
#   → http://127.0.0.1:8793/
#   → health check: http://127.0.0.1:8793/healthz
```

`gh auth status` must be valid for the enrichment step. Everything else is
Python standard library only (tested on Python 3.12 and 3.14).

---

## Layout

```
github-discovery-archive/
├── sync.py                     # state → data/projects.json (+ metadata cache)
├── serve.py                    # stdlib static server, security headers, /healthz
├── data/
│   ├── projects.json           # generated dataset consumed by the site
│   └── metadata_cache.json     # generated GitHub metadata cache
├── web/
│   ├── index.html              # app shell (Chinese-first)
│   └── assets/
│       ├── styles.css          # dark-native design system
│       └── app.js              # views, filters, search, charts (vanilla JS)
├── tests/
│   ├── test_sync.py            # parsing, dedupe, schema, atomic write
│   ├── test_server.py          # health, headers, 404, path safety
│   └── fixtures/state_sample.json
├── deploy/                     # systemd unit templates + deploy notes
└── README.md
```

---

## Data model (`data/projects.json`)

```jsonc
{
  "generated_at": "2026-10-29T04:15:35Z",
  "source_version": 1,
  "source_created_at": "2026-06-05",
  "preferences": { "themes": [...], "signals": [...] },
  "stats": {
    "total_records": 550, "unique_projects": 544, "total_days": 109,
    "date_min": "2026-06-05", "date_max": "2026-10-29",
    "enriched_ok": 541, "enriched_total": 544,
    "stars_total": 8132435, "stars_known": 541,
    "languages": [{ "name": "Python", "count": 174 }, ...],
    "tags":      [{ "name": "self-hosted", "count": 224 }, ...],
    "topics":    [...], "top_owners": [...],
    "all_tags": [...], "all_languages": [...]
  },
  "days": [                       // newest → oldest, one entry per recommendation day
    { "date": "2026-10-29", "projects": ["owner/repo", ...],
      "count": 5, "preference_summary": "…" }
  ],
  "projects": [                   // one entry per unique repo (deduped)
    {
      "full_name": "storytold/photocraft",
      "owner": "storytold", "name": "photocraft",
      "url": "https://github.com/storytold/photocraft",
      "reason_tags": ["creative-tools", "rust-wgpu", "offline-first"],
      "recommendation_dates": ["2026-10-29"],   // full history
      "recommendation_count": 1,
      "first_recommended": "2026-10-29",
      "last_recommended": "2026-10-29",
      "github": {
        "status": "ok",           // "ok" | "not_found" | "error" | "unknown"
        "description": "…", "stars": 74492, "forks": 13161, "watchers": 322,
        "language": "Rust", "language_color": "#dea584", "license": "Apache-2.0",
        "topics": ["images", "photo-editing", ...],
        "archived": false, "fork": false,
        "open_issues": 2261, "open_pulls": 1169, "default_branch": "main",
        "created_at": "…", "updated_at": "…", "pushed_at": "…",
        "fetched_at": "2026-10-29T04:15:33Z"
      }
    }
  ]
}
```

**Missing repos are handled gracefully**: a repository GitHub can no longer
resolve is stored with `github.status == "not_found"` and `null` metrics; a
transient transport failure is `"error"` (and is *not* cached, so the next run
retries it). The site renders both without breaking.

---

## Sync behaviour

- **Dedupe** by `full_name`, keeping the *ordered union* of reason tags and the
  sorted list of every date the repo was recommended (`recommendation_dates`).
- **Batched GraphQL**: 25 repos per `gh api graphql` call, one aliased
  `repository` per repo. Missing repos come back as `null` and are marked
  `not_found`.
- **Local cache** (`data/metadata_cache.json`) with a 24 h TTL keeps daily runs
  fast; `--refresh` forces a full refetch. Only `ok`/`not_found` results are
  cached — hard failures always retry.
- **Atomic writes**: dataset and cache are written to a temp file in the same
  directory and `os.replace`-d into place, so readers never see a partial file.

```bash
python3 sync.py                 # incremental (uses cache)
python3 sync.py --refresh       # refetch everything
python3 sync.py --offline       # no network; cache-only, gaps marked "unknown"
python3 sync.py --cache-ttl 3600
python3 sync.py --state /path/to/state.json --output /tmp/p.json --quiet
```

### Cron integration

The sync runs immediately whenever the recommendation state changes via
`deploy/github-discovery-sync.path`; `deploy/github-discovery-sync.timer` is a
daily catch-up safety net. See `deploy/README.md`.

---

## Website features

- **Timeline** view: latest-day hero carousel + every day grouped by date, each
  with its preference summary (collapsible) and pick cards.
- **Gallery** view: flat, sortable grid of all matching projects.
- **Stats** view: KPI cards, language distribution, top tags, daily-activity
  heatmap with legend, top-starred repos, most-recommended repos — all rendered
  with CSS bars and inline SVG/CSS (no chart library).
- **Search** across name, owner, description, topics, tags and language.
- **Filters**: by tag (top 40), language, and flags (多次推荐 / 已归档), with
  removable active-filter chips and a count badge.
- **Sort**: recency, stars, name, last push, recommendation count.
- **Detail dialog**: stars/forks/license/issues, tags, topics, recommendation
  dates, the day's summary, and a direct **GitHub** link.
- **UX**: loading skeletons, empty and error states (with retry), responsive
  down to mobile, keyboard support (`/` focuses search, `Esc` closes the
  dialog), `aria-*` roles, skip link, and shareable URL hash state.

---

## Server (`serve.py`)

- Serves `web/` as the document root and exposes `/data/projects.json`.
- `GET /healthz` → `{"status":"ok","service":"github-discovery-archive", ...}`.
- Security headers on every response: `X-Content-Type-Options`, `X-Frame-Options:
  DENY`, `Referrer-Policy`, `Permissions-Policy`, `Cross-Origin-Opener-Policy`
  and a restrictive `Content-Security-Policy`.
- Correct MIME types (+ `charset=utf-8`), gzip when requested, no directory
  listing, and path-traversal protection (project source files are unreachable).

---

## Tests

```bash
python3 -m unittest discover -s tests -v
```

Covers state parsing (valid + invalid), dedupe/history preservation, tag-union
ordering, day grouping, GraphQL query building, repository-node normalisation,
the output schema, atomic writes, an offline end-to-end sync, and the server
(health endpoint, security headers, MIME types, 404s, HEAD, and path safety).

---

## Deployment

See [`deploy/README.md`](deploy/README.md). The web server binds
`127.0.0.1:8793`; the parent deployment terminates TLS for
`projects.ximilala.com` and reverse-proxies to it.
