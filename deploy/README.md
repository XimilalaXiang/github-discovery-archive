# Deployment (systemd)

Four units, plus the optional reverse-proxy hop you already run for
`projects.ximilala.com`.

| Unit | Role |
|------|------|
| `github-discovery-archive.service` | Long-running static server on `127.0.0.1:8793` |
| `github-discovery-sync.service` | Oneshot: state → `data/projects.json` |
| `github-discovery-sync.path` | Runs the sync whenever the recommendation state changes |
| `github-discovery-sync.timer` | Daily catch-up safety net |

## Install

```bash
sudo cp deploy/github-discovery-archive.service /etc/systemd/system/
sudo cp deploy/github-discovery-sync.service     /etc/systemd/system/
sudo cp deploy/github-discovery-sync.path        /etc/systemd/system/
sudo cp deploy/github-discovery-sync.timer       /etc/systemd/system/

sudo systemctl daemon-reload
sudo systemctl enable --now github-discovery-archive.service
sudo systemctl enable --now github-discovery-sync.path
sudo systemctl enable --now github-discovery-sync.timer

# One-off sync now, and tail logs
sudo systemctl start github-discovery-sync.service
journalctl -u github-discovery-archive -f
```

## Verify

```bash
curl -s localhost:8793/healthz          # {"status": "ok", ...}
curl -sI localhost:8793/ | head         # security headers
```

## Reverse proxy

The server binds loopback only. Point the existing Cloudflare tunnel / nginx
route for `projects.ximilala.com` at `http://127.0.0.1:8793`. The server already
serves `gzip` when the client asks for it and sets `Vary: Accept-Encoding`, so
upstream compression is optional.

## Notes

- `gh` must be authenticated for the user running the sync service
  (`gh auth status`). The sync calls `gh api graphql` in batches of 25.
- The units assume the repo lives at `/home/ubuntu/github-discovery-archive`
  and run as user `ubuntu`. Adjust paths/user if you relocate it.
- `ProtectHome=read-only` is intentional: the sync only needs read access to
  `~/.hermes/cron/state/` and the `gh` config, and write access to
  `data/` (granted via `ReadWritePaths`).
