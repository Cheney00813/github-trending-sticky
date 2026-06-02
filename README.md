# GitHub Trending Desktop Sticky Note

🖥️ A cyberpunk-themed desktop sticky note widget that displays daily trending GitHub projects. Fetches real data via GitHub API, updates automatically every 6 hours.

![screenshot](https://img.shields.io/badge/theme-cyberpunk%20terminal-brightgreen) ![python](https://img.shields.io/badge/python-3.9%2B-blue) ![platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey)

## Features

- 🎨 **Cyberpunk terminal aesthetic** — dark theme, neon green/cyan/amber accents, scanline overlay, CRT vignette
- 🔄 **Auto-updating** — background scheduler runs every 6 hours + on login
- 🚀 **Auto-start** — shortcut in startup folder, sticky note appears on boot
- 🛡 **Fault-tolerant** — retry with exponential backoff, cache fallback, freshness tracking
- 🔗 **Clickable repos** — each repo name links directly to GitHub
- 📦 **Zero external deps** — Python stdlib + `gh` CLI only

## Quick Start

### Prerequisites

```bash
python --version   # 3.9+
gh --version       # GitHub CLI
gh auth status     # Must be logged in
```

### Install

```bash
# Clone the repo
git clone https://github.com/Cheney00813/github-trending-sticky.git
cd github-trending-sticky

# Run once to verify
python fetch_trending.py

# Launch (Windows)
open_sticky.bat

# Launch (macOS/Linux)
chmod +x open_sticky.sh
./open_sticky.sh
```

### Auto-start on boot

**Windows**: A shortcut is automatically created in the Startup folder.
**macOS**: System Preferences → Users & Groups → Login Items → Add `open_sticky.sh`
**Linux**: Add to `~/.config/autostart/`

## Architecture

```
open_sticky.bat / .sh
├── scheduler.py (background, persistent)
│   ├── On startup → fetch_trending.py → sticky.html
│   ├── Every 6h   → fetch_trending.py → sticky.html
│   └── State tracked in scheduler_state.json
└── sticky.html (browser, auto-refresh every 2h)
```

## Configuration

Edit constants at the top of the scripts:

| Setting | Default | File |
|---------|---------|------|
| `MAX_REPOS` | 10 | `fetch_trending.py` |
| `SEARCH_WINDOW_DAYS` | 7 | `fetch_trending.py` |
| `MAX_RETRIES` | 3 | `fetch_trending.py` |
| `UPDATE_INTERVAL_HOURS` | 6 | `scheduler.py` |

## Error Handling

Custom exception hierarchy with graceful degradation:

```
StickyError
├── PreflightError (gh CLI missing, dir not writable)
├── ApiError (4xx, 5xx, rate limits)
│   └── RateLimitError
├── NetworkError (timeout, DNS, connection refused)
├── ParseError (invalid JSON)
└── InvalidResponseError (missing fields)
```

- **Retry**: 3 attempts with exponential backoff (2s → 4s → 8s)
- **Cache fallback**: Stale data served when API is unavailable
- **Partial failure**: Individual repo parse failures don't abort the batch
- **Atomic writes**: `.tmp` → `os.replace()` prevents corruption

## Project Structure

```
github-sticky/
├── fetch_trending.py      # Core: fetch API → validate → generate HTML
├── scheduler.py           # Background scheduler with job state machine
├── open_sticky.bat        # Windows launcher
├── open_sticky.sh         # Unix launcher
├── schedule_task.xml      # Windows Task Scheduler XML (alternative)
├── install_task.bat       # Task Scheduler installer
└── .gitignore
```

## License

MIT
