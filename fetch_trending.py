#!/usr/bin/env python3
"""
GitHub Trending Projects Fetcher
Fetches trending repos via gh CLI and generates a sticky note HTML file.

Design:
  - Custom exception hierarchy for every failure mode
  - Pre-flight validation (gh CLI, output dir)
  - Retry with exponential backoff on transient failures
  - Cache with freshness tracking as graceful degradation
  - Exception chaining preserves root cause for debugging
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

# ═══════════════════════════════════════════════════════════════════
# Configuration
# ═══════════════════════════════════════════════════════════════════

SCRIPT_DIR: str = os.path.dirname(os.path.abspath(__file__))
OUTPUT_HTML: str = os.path.join(SCRIPT_DIR, "sticky.html")
CACHE_FILE: str = os.path.join(SCRIPT_DIR, "cache.json")
MAX_REPOS: int = 10
SEARCH_WINDOW_DAYS: int = 7
MAX_RETRIES: int = 3
RETRY_BASE_DELAY: float = 2.0  # seconds, doubles each retry


# ═══════════════════════════════════════════════════════════════════
# Exception Hierarchy
# ═══════════════════════════════════════════════════════════════════

class StickyError(Exception):
    """Base exception for all sticky-note errors."""


class PreflightError(StickyError):
    """Pre-flight check failed — cannot even attempt the operation."""


class GhCliNotFoundError(PreflightError):
    """gh CLI is not installed or not on PATH."""


class OutputDirNotWritableError(PreflightError):
    """Cannot write output file."""


class ApiError(StickyError):
    """GitHub API returned an error response."""

    def __init__(self, message: str, status_code: int | None = None, body: str = "") -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(message)


class RateLimitError(ApiError):
    """API rate limit exceeded."""


class NetworkError(StickyError):
    """Network-level failure (DNS, timeout, connection refused)."""


class ParseError(StickyError):
    """Response could not be parsed as valid JSON."""


class InvalidResponseError(StickyError):
    """API response parsed but missing required fields."""


class CacheError(StickyError):
    """Cache read/write failed (non-fatal, logged as warning)."""

    def __init__(self, message: str, fatal: bool = False) -> None:
        self.fatal = fatal
        super().__init__(message)


# ═══════════════════════════════════════════════════════════════════
# Domain Types
# ═══════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class RepoInfo:
    """Immutable domain representation of a trending repository."""
    full_name: str
    url: str
    description: str
    stars: int
    language: str
    forks: int
    topics: list[str] = field(default_factory=list)
    created_at: str = ""

    @classmethod
    def from_api_item(cls, item: dict[str, Any]) -> RepoInfo:
        """Parse and validate a single API response item.

        Raises InvalidResponseError if required fields are missing.
        """
        missing: list[str] = []
        for field in ("full_name", "html_url", "stargazers_count"):
            if field not in item:
                missing.append(field)

        if missing:
            raise InvalidResponseError(
                f"API item missing required fields: {', '.join(missing)}. "
                f"Got keys: {sorted(item.keys())}"
            )

        return cls(
            full_name=item["full_name"],
            url=item["html_url"],
            description=(item.get("description") or "No description"),
            stars=item["stargazers_count"],
            language=(item.get("language") or "N/A"),
            forks=item.get("forks_count", 0),
            topics=list(item.get("topics", [])[:5]),
            created_at=item.get("created_at", ""),
        )


# ═══════════════════════════════════════════════════════════════════
# Pre-flight Validation
# ═══════════════════════════════════════════════════════════════════

def _check_gh_cli() -> None:
    """Verify gh CLI is installed and authenticated.

    Raises:
        GhCliNotFoundError: If gh is missing or unauthenticated.
    """
    result = subprocess.run(
        ["gh", "auth", "status"],
        capture_output=True, text=True, encoding="utf-8", timeout=10,
    )
    if result.returncode != 0:
        raise GhCliNotFoundError(
            "gh CLI is not installed or not authenticated. "
            "Install from https://cli.github.com/ and run 'gh auth login'."
        )


def _check_output_dir() -> None:
    """Verify the output directory is writable."""
    if not os.path.isdir(SCRIPT_DIR):
        raise OutputDirNotWritableError(
            f"Script directory does not exist: {SCRIPT_DIR}"
        )
    test_path = os.path.join(SCRIPT_DIR, ".write_test")
    try:
        with open(test_path, "w") as f:
            f.write("ok")
        os.remove(test_path)
    except OSError as e:
        raise OutputDirNotWritableError(
            f"Cannot write to {SCRIPT_DIR}: {e}"
        ) from e


# ═══════════════════════════════════════════════════════════════════
# Data Fetching (with retry + cache fallback)
# ═══════════════════════════════════════════════════════════════════

def _call_gh_api(endpoint: str) -> str:
    """Call gh api with retry on transient failures.

    Args:
        endpoint: API endpoint path (e.g. "/search/repositories?q=...")

    Returns:
        Raw JSON response body as string.

    Raises:
        ApiError: On 4xx/5xx responses.
        RateLimitError: On 429 with retry-after exhausted.
        NetworkError: On connection/timeout failures after all retries.
        TimeoutError: If the subprocess times out.
    """
    last_error: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            result = subprocess.run(
                ["gh", "api", "-H", "Accept: application/vnd.github+json", endpoint],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=30,
            )
        except subprocess.TimeoutExpired as e:
            raise NetworkError(
                f"gh api timed out after {e.timeout}s on attempt {attempt}"
            ) from e
        except FileNotFoundError as e:
            raise GhCliNotFoundError(
                "gh CLI not found. Is GitHub CLI installed?"
            ) from e
        except OSError as e:
            last_error = NetworkError(
                f"gh api OS error on attempt {attempt}: {e}"
            )
            last_error.__cause__ = e
            if attempt < MAX_RETRIES:
                delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
                print(f"[WARN] Retrying in {delay:.0f}s (attempt {attempt}/{MAX_RETRIES})...")
                time.sleep(delay)
            continue

        if result.returncode == 0:
            return result.stdout or ""

        stderr_text = result.stderr.strip()

        # Detect rate limiting
        if "rate limit" in stderr_text.lower() or "429" in stderr_text:
            if attempt < MAX_RETRIES:
                delay = RETRY_BASE_DELAY * (2 ** (attempt - 1)) * 3  # longer for rate limits
                print(f"[WARN] Rate limited. Waiting {delay:.0f}s (attempt {attempt}/{MAX_RETRIES})...")
                time.sleep(delay)
                continue
            raise RateLimitError(
                f"GitHub API rate limit exceeded after {MAX_RETRIES} retries. "
                "Wait ~1 hour or set GITHUB_TOKEN for higher limits."
            )

        # Non-retryable client errors
        if 400 <= result.returncode < 500 and result.returncode != 429:
            raise ApiError(
                f"GitHub API client error (attempt {attempt})",
                status_code=result.returncode,
                body=stderr_text,
            )

        # Server errors → retry
        last_error = ApiError(
            f"GitHub API returned error (attempt {attempt})",
            status_code=result.returncode,
            body=stderr_text,
        )
        if attempt < MAX_RETRIES:
            delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
            print(f"[WARN] Server error. Retrying in {delay:.0f}s (attempt {attempt}/{MAX_RETRIES})...")
            time.sleep(delay)
        continue

    # Exhausted all retries
    assert last_error is not None
    raise last_error


def fetch_trending_repos(since_days: int = SEARCH_WINDOW_DAYS, count: int = MAX_REPOS,
                          language: str | None = None) -> list[RepoInfo]:
    """Fetch trending repositories from GitHub API.

    Args:
        since_days: How many days back to search (default 7).
        count: Number of repos to fetch (default 10, max 100).
        language: Optional language filter (e.g. 'Python', 'Rust').

    Returns:
        List of validated RepoInfo objects, sorted by stars desc.

    Raises:
        ApiError: On non-retryable API errors.
        NetworkError: On network failures after all retries.
        ParseError: If response is not valid JSON.
        InvalidResponseError: If API response is missing required fields.
    """
    since_date = (datetime.now(timezone.utc) - timedelta(days=since_days)).strftime("%Y-%m-%d")

    query = f"created:>{since_date}"
    if language:
        query += f"+language:{language}"

    endpoint = (
        "/search/repositories"
        f"?q={query}"
        "&sort=stars"
        "&order=desc"
        f"&per_page={min(count, 100)}"
    )

    print(f"[INFO] Fetching repos created since {since_date}...")
    if language:
        print(f"[INFO] Language filter: {language}")
    raw_json = _call_gh_api(endpoint)

    # ── Parse JSON with explicit error context ──
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError as e:
        raise ParseError(
            f"GitHub API returned invalid JSON: {e}. "
            f"First 200 chars of response: {raw_json[:200]!r}"
        ) from e

    # ── Validate response structure ──
    if not isinstance(data, dict):
        raise InvalidResponseError(
            f"Expected JSON object, got {type(data).__name__}"
        )
    if "items" not in data:
        raise InvalidResponseError(
            f"API response missing 'items' key. "
            f"Top-level keys: {sorted(data.keys())}. "
            f"Message: {data.get('message', 'no message')}"
        )

    # ── Parse each item, tracking partial failures ──
    repos: list[RepoInfo] = []
    parse_failures: list[tuple[int, str]] = []

    for idx, item in enumerate(data["items"]):
        try:
            repos.append(RepoInfo.from_api_item(item))
        except InvalidResponseError as e:
            parse_failures.append((idx, str(e)))

    if parse_failures:
        failure_detail = "; ".join(
            f"item[{i}]: {msg}" for i, msg in parse_failures
        )
        print(f"[WARN] {len(parse_failures)} item(s) failed to parse: {failure_detail}")

    if not repos:
        raise InvalidResponseError(
            f"All {len(data['items'])} items failed validation. "
            f"First failure: {parse_failures[0][1] if parse_failures else 'unknown'}"
        )

    print(f"[INFO] Fetched {len(repos)} trending repos ({(len(repos) / len(data['items']) * 100):.0f}% valid).")
    _save_cache(repos)
    return repos


# ═══════════════════════════════════════════════════════════════════
# Cache (with freshness metadata)
# ═══════════════════════════════════════════════════════════════════

@dataclass
class CacheEntry:
    """Cache wrapper with freshness metadata."""
    repos: list[dict[str, Any]]
    cached_at: str
    ttl_hours: int = 24

    @property
    def is_stale(self) -> bool:
        """True if cache is older than TTL."""
        try:
            cached_time = datetime.fromisoformat(self.cached_at)
            age = datetime.now(timezone.utc) - cached_time.replace(tzinfo=timezone.utc)
            return age > timedelta(hours=self.ttl_hours)
        except (ValueError, TypeError):
            return True


def _load_cache() -> list[RepoInfo]:
    """Load cached results, returning empty list if unavailable.

    Never raises — cache failures are always non-fatal.
    """
    if not os.path.exists(CACHE_FILE):
        return []

    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"[WARN] Cache read failed: {e}")
        return []

    if isinstance(raw, dict) and "repos" in raw:
        # New format with metadata
        entry = CacheEntry(
            repos=raw["repos"],
            cached_at=raw.get("cached_at", "unknown"),
            ttl_hours=raw.get("ttl_hours", 24),
        )
        items = entry.repos
        age_tag = "[STALE]" if entry.is_stale else "[fresh]"
        print(f"[WARN] Using cached data {age_tag} (cached: {entry.cached_at})")
    elif isinstance(raw, list):
        # Legacy format
        items = raw
        print("[WARN] Using cached data (legacy format, no freshness info).")
    else:
        print("[WARN] Cache format unrecognized. Ignoring.")
        return []

    # Parse cached items, skipping bad entries
    repos: list[RepoInfo] = []
    for item in items:
        try:
            repos.append(RepoInfo.from_api_item(item))
        except InvalidResponseError:
            continue

    return repos


def _save_cache(repos: list[RepoInfo]) -> None:
    """Save results to cache with freshness metadata.

    Cache failures are logged but never propagated.
    """
    cache_data: dict[str, Any] = {
        "repos": [
            {
                "full_name": r.full_name,
                "html_url": r.url,
                "description": r.description,
                "stargazers_count": r.stars,
                "language": r.language,
                "forks_count": r.forks,
                "topics": r.topics,
                "created_at": r.created_at,
            }
            for r in repos
        ],
        "cached_at": datetime.now(timezone.utc).isoformat(),
        "ttl_hours": 24,
    }

    try:
        tmp_path = CACHE_FILE + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(cache_data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, CACHE_FILE)  # Atomic write
    except OSError as e:
        print(f"[WARN] Cache save failed (non-fatal): {e}")


# ═══════════════════════════════════════════════════════════════════
# HTML Generation
# ═══════════════════════════════════════════════════════════════════

def _escape_html(text: str | None) -> str:
    """Escape text for safe HTML rendering."""
    if not text:
        return ""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _topic_tags(topics: list[str]) -> str:
    """Generate HTML span elements for topic tags."""
    if not topics:
        return ""
    tags = "".join(
        f'<span class="topic-tag">{_escape_html(t)}</span>' for t in topics
    )
    return f'<div class="topics">{tags}</div>'


_LANG_COLORS: dict[str, str] = {
    "Python": "#3572A5", "JavaScript": "#f1e05a", "TypeScript": "#3178c6",
    "Go": "#00ADD8", "Rust": "#dea584", "Java": "#b07219",
    "C++": "#f34b7d", "C": "#555555", "Ruby": "#701516",
    "Swift": "#F05138", "Kotlin": "#A97BFF", "C#": "#178600",
    "Zig": "#ec915c", "Lua": "#000080", "N/A": "#888888",
    "Shell": "#89e051", "Vue": "#41b883", "Svelte": "#ff3e00",
    "Dart": "#00B4AB", "PHP": "#4F5D95", "R": "#198CE7",
    "Scala": "#c22d40", "Elixir": "#6e4a7e", "Clojure": "#db5855",
}


def _lang_color(language: str) -> str:
    """Map programming language to a representative hex color."""
    return _LANG_COLORS.get(language, "#888888")


def _format_stars(stars: int) -> str:
    """Format star count with k/m suffix for readability."""
    if stars >= 1_000_000:
        return f"{stars / 1_000_000:.1f}M"
    if stars >= 1_000:
        return f"{stars / 1_000:.1f}k"
    return str(stars)


def _relative_time(iso_string: str) -> str:
    """Convert ISO datetime to relative time display (e.g. '2d ago', '12h ago')."""
    if not iso_string:
        return ""
    try:
        created = datetime.fromisoformat(iso_string.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - created.replace(tzinfo=timezone.utc)
        if delta.days > 0:
            return f"{delta.days}d ago"
        hours = delta.seconds // 3600
        if hours > 0:
            return f"{hours}h ago"
        minutes = delta.seconds // 60
        return f"{minutes}m ago"
    except (ValueError, TypeError):
        return ""


def generate_html(repos: list[RepoInfo], since_days: int = SEARCH_WINDOW_DAYS,
                   language: str | None = None) -> str:
    """Generate a self-contained HTML sticky note page with Cyberpunk Terminal aesthetic."""
    update_time = datetime.now().strftime("%Y-%m-%d %H:%M")
    lang_tag = f" lang:{language}" if language else ""

    repo_cards_parts: list[str] = []
    for i, repo in enumerate(repos):
        lang_dot = _lang_color(repo.language)
        desc = _escape_html(repo.description)
        name = _escape_html(repo.full_name)
        url = _escape_html(repo.url)
        stars_display = _format_stars(repo.stars)
        topics_html = _topic_tags(repo.topics)
        delay = 0.05 * i
        rel_time = _relative_time(repo.created_at)
        time_html = f'<span class="meta-item time" title="{_escape_html(repo.created_at)}">{_escape_html(rel_time)}</span>' if rel_time else ""

        repo_cards_parts.append(f"""
            <div class="repo-card" style="animation-delay:{delay:.2f}s">
                <span class="rank-badge">{"{:02d}".format(i + 1)}</span>
                <div class="repo-body">
                    <a class="repo-name" href="{url}" target="_blank">{name}</a>
                    <span class="repo-desc">{desc}</span>
                    {topics_html}
                    <div class="repo-meta">
                        <span class="meta-item"><span class="lang-dot" style="background:{lang_dot}"></span>{_escape_html(repo.language)}</span>
                        <span class="meta-item sep">|</span>
                        <span class="meta-item star">* {stars_display}</span>
                        <span class="meta-item fork">v {repo.forks:,}</span>
                        {time_html}
                    </div>
                </div>
            </div>""")

    repo_cards = "".join(repo_cards_parts)

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="refresh" content="7200">
<title>GH Trending</title>
<script>(function(){{var w=420,h=720,sw=screen.availWidth,sh=screen.availHeight;window.resizeTo(w,h);window.moveTo(Math.round((sw-w)/2),Math.round((sh-h)/2));}})();
document.addEventListener('click',function(e){{var a=e.target.closest('a');if(a&&a.href){{e.preventDefault();window.open(a.href,'_blank');}}}});</script>
<style>
:root {{
    --bg-deep: #06090f;
    --bg-panel: #0d1117;
    --bg-card: #131822;
    --border: #1e2a3a;
    --border-glow: #1f3d5c;
    --text-primary: #c9d1d9;
    --text-secondary: #8b949e;
    --text-dim: #5d6a7a;
    --neon-green: #3bf04b;
    --neon-cyan: #01c8ee;
    --neon-amber: #f0a33b;
    --neon-pink: #f750b6;
    --glow-green: rgba(59, 240, 75, 0.15);
    --glow-cyan: rgba(1, 200, 238, 0.12);
    --font-mono: "Cascadia Code", "JetBrains Mono", "Fira Code", "Consolas", "SF Mono", monospace;
    --font-sans: "Segoe UI", "Microsoft YaHei", system-ui, sans-serif;
}}

* {{ margin: 0; padding: 0; box-sizing: border-box; }}

body {{
    background: var(--bg-deep);
    color: var(--text-primary);
    font-family: var(--font-sans);
    min-height: 100vh;
    padding: 16px 14px;
    user-select: none;
    overflow-y: auto;
    background-image:
        repeating-linear-gradient(0deg, transparent, transparent 2px,
            rgba(0,0,0,0.08) 2px, rgba(0,0,0,0.08) 3px);
    box-shadow: inset 0 0 80px rgba(0,0,0,0.5);
}}

.terminal-chrome {{
    display: flex; align-items: center; gap: 8px;
    padding: 6px 10px; margin-bottom: 14px;
    background: rgba(255,255,255,0.02);
    border: 1px solid var(--border); border-radius: 6px;
    font-family: var(--font-mono); font-size: 10px; color: var(--text-dim);
}}
.chrome-dots {{ display: flex; gap: 6px; flex-shrink: 0; }}
.chrome-dot {{ width: 8px; height: 8px; border-radius: 50%; opacity: 0.7; }}
.chrome-dot.r {{ background: #ff5f57; }}
.chrome-dot.y {{ background: #febc2e; }}
.chrome-dot.g {{ background: #28c840; }}
.chrome-title {{ flex: 1; text-align: center; color: var(--text-dim); letter-spacing: 0.5px; }}
.chrome-status {{ color: var(--neon-green); opacity: 0.6; animation: status-pulse 2s ease-in-out infinite; }}

@keyframes status-pulse {{ 0%,100%{{opacity:0.4}} 50%{{opacity:0.9}} }}

.header {{ text-align: center; margin-bottom: 14px; position: relative; }}
.header-icon {{ font-size: 28px; margin-bottom: 2px; filter: drop-shadow(0 0 8px var(--neon-green)); animation: icon-float 3s ease-in-out infinite; }}

@keyframes icon-float {{ 0%,100%{{transform:translateY(0)}} 50%{{transform:translateY(-5px)}} }}

.header-title {{
    font-family: var(--font-mono); font-size: 15px; font-weight: 700;
    color: var(--neon-green);
    text-shadow: 0 0 20px var(--glow-green), 0 0 40px var(--glow-green);
    letter-spacing: 2px; text-transform: uppercase;
}}
.header-prompt {{ display: inline-block; color: var(--neon-cyan); margin-right: 2px; }}
.header-subtitle {{ font-family: var(--font-mono); font-size: 9.5px; color: var(--text-dim); margin-top: 4px; letter-spacing: 0.5px; }}
.header-subtitle .highlight {{ color: var(--neon-amber); }}

.repo-list {{ display: flex; flex-direction: column; gap: 6px; }}

.repo-card {{
    display: flex; gap: 10px; align-items: flex-start;
    background: var(--bg-card); border: 1px solid var(--border);
    border-radius: 6px; padding: 10px 11px; position: relative;
    transition: border-color 0.2s, background 0.2s, transform 0.15s;
    animation: card-in 0.4s ease-out both; overflow: hidden;
}}
.repo-card::before {{
    content: ""; position: absolute; left: 0; top: 8px; bottom: 8px;
    width: 2px; border-radius: 1px; background: transparent;
    transition: background 0.25s, box-shadow 0.25s;
}}
.repo-card:hover {{ border-color: var(--border-glow); background: #161e2a; transform: translateX(3px); }}
.repo-card:hover::before {{ background: var(--neon-cyan); box-shadow: 0 0 10px var(--neon-cyan), 0 0 20px rgba(1,200,238,0.4); }}

@keyframes card-in {{ from {{ opacity:0; transform:translateY(8px); }} to {{ opacity:1; transform:translateY(0); }} }}

.rank-badge {{
    font-family: var(--font-mono); font-size: 14px; font-weight: 700;
    color: var(--neon-green); min-width: 28px; height: 28px;
    display: flex; align-items: center; justify-content: center;
    border: 1px solid rgba(59,240,75,0.2); border-radius: 4px;
    background: rgba(59,240,75,0.04); flex-shrink: 0;
    text-shadow: 0 0 8px var(--glow-green);
}}
.repo-card:nth-child(1) .rank-badge {{ border-color: rgba(247,80,182,0.4); color: var(--neon-pink); text-shadow: 0 0 10px rgba(247,80,182,0.5); background: rgba(247,80,182,0.06); }}
.repo-card:nth-child(2) .rank-badge {{ border-color: rgba(240,163,59,0.35); color: var(--neon-amber); text-shadow: 0 0 8px rgba(240,163,59,0.4); }}
.repo-card:nth-child(3) .rank-badge {{ border-color: rgba(1,200,238,0.3); color: var(--neon-cyan); text-shadow: 0 0 8px rgba(1,200,238,0.4); }}

.repo-body {{ flex: 1; min-width: 0; }}

.repo-name {{
    font-family: var(--font-mono); font-size: 12.5px; font-weight: 600;
    color: #58a6ff; text-decoration: none; display: block;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    transition: color 0.15s; letter-spacing: -0.2px;
}}
.repo-name:hover {{ color: var(--neon-cyan); text-decoration: underline; text-underline-offset: 3px; }}

.repo-desc {{
    display: block; font-size: 10.5px; color: var(--text-secondary);
    margin-top: 3px; line-height: 1.45;
    display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
}}

.topics {{ display: flex; flex-wrap: wrap; gap: 4px; margin-top: 5px; }}
.topic-tag {{
    font-family: var(--font-mono); font-size: 8.5px;
    padding: 1px 6px; background: rgba(1,200,238,0.06);
    color: var(--neon-cyan); border: 1px solid rgba(1,200,238,0.15);
    border-radius: 3px; white-space: nowrap; letter-spacing: 0.3px;
}}

.repo-meta {{ display: flex; gap: 8px; margin-top: 5px; font-family: var(--font-mono); font-size: 9.5px; color: var(--text-dim); align-items: center; flex-wrap: wrap; }}
.meta-item {{ display: inline-flex; align-items: center; gap: 4px; }}
.meta-item.sep {{ color: var(--border); font-size: 8px; }}
.meta-item.star {{ color: var(--neon-amber); }}
.meta-item.fork {{ color: var(--text-dim); }}

.lang-dot {{ display: inline-block; width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0; box-shadow: 0 0 4px currentColor; }}

.footer {{ text-align: center; margin-top: 14px; padding-top: 10px; border-top: 1px solid var(--border); font-family: var(--font-mono); font-size: 8.5px; color: var(--text-dim); line-height: 1.6; letter-spacing: 0.3px; }}
.footer .cmd {{ color: var(--neon-green); opacity: 0.7; }}
.footer .blink {{ display: inline-block; width: 6px; height: 11px; background: var(--neon-green); opacity: 0.8; margin-left: 2px; vertical-align: text-bottom; animation: blink 1s step-end infinite; }}

@keyframes blink {{ 0%,100%{{opacity:1}} 50%{{opacity:0}} }}

::-webkit-scrollbar {{ width: 4px; }}
::-webkit-scrollbar-track {{ background: transparent; }}
::-webkit-scrollbar-thumb {{ background: rgba(1,200,238,0.12); border-radius: 4px; }}
::-webkit-scrollbar-thumb:hover {{ background: rgba(1,200,238,0.3); }}

::selection {{ background: rgba(1,200,238,0.25); color: var(--neon-cyan); }}
</style>
</head>
<body>

<div class="terminal-chrome">
    <div class="chrome-dots">
        <span class="chrome-dot r"></span><span class="chrome-dot y"></span><span class="chrome-dot g"></span>
    </div>
    <span class="chrome-title">trending.sh</span>
    <span class="chrome-status">● LIVE</span>
</div>

<div class="header">
    <div class="header-icon">◈</div>
    <div class="header-title"><span class="header-prompt">$</span> github-trending</div>
    <div class="header-subtitle">
        repos created since <span class="highlight">--since={since_days}d{lang_tag}</span>
         ·  refreshed <span class="highlight">{update_time}</span>
    </div>
</div>

<div class="repo-list">
    {repo_cards}
</div>

<div class="footer">
    <span class="cmd">$ crontab -l</span><br>
    "0 9 * * *  /usr/bin/gh-trending --update"<br>
    <span class="cmd">click repo name → open in github</span><span class="blink"></span>
</div>

</body>
</html>"""


# ═══════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════

def main() -> None:
    """Orchestrate the fetch-and-generate pipeline with error handling.

    Exit codes:
        0 — Success
        1 — Recoverable error (cache fallback used)
        2 — Fatal error (no output generated)
    """
    # ── CLI argument parsing (stdlib only, no argparse overhead) ──
    args = sys.argv[1:]
    language: str | None = None
    since_days = SEARCH_WINDOW_DAYS
    count = MAX_REPOS
    output_path = OUTPUT_HTML
    i = 0
    while i < len(args):
        if args[i] in ("--language", "-l") and i + 1 < len(args):
            language = args[i + 1]; i += 2
        elif args[i] in ("--since", "-s") and i + 1 < len(args):
            try: since_days = int(args[i + 1])
            except ValueError: print(f"[WARN] Invalid --since value, using {since_days}", file=sys.stderr)
            i += 2
        elif args[i] in ("--count", "-n") and i + 1 < len(args):
            try: count = int(args[i + 1])
            except ValueError: print(f"[WARN] Invalid --count value, using {count}", file=sys.stderr)
            i += 2
        elif args[i] in ("--output", "-o") and i + 1 < len(args):
            output_path = args[i + 1]; i += 2
        elif args[i] in ("--help", "-h"):
            print("Usage: python fetch_trending.py [OPTIONS]")
            print("  -l, --language LANG   Filter by language (e.g. Python, Rust)")
            print(f"  -s, --since DAYS      Days to look back (default: {SEARCH_WINDOW_DAYS})")
            print(f"  -n, --count N         Number of repos (default: {MAX_REPOS})")
            print(f"  -o, --output PATH     Output HTML file (default: sticky.html)")
            print("  -h, --help            Show this help")
            sys.exit(0)
        else:
            i += 1

    # ── Pre-flight checks ──
    try:
        _check_gh_cli()
        _check_output_dir()
    except PreflightError as e:
        print(f"[FATAL] Pre-flight check failed: {e}", file=sys.stderr)
        sys.exit(2)

    # ── Fetch (with cache fallback) ──
    repos: list[RepoInfo] = []
    used_cache = False

    try:
        repos = fetch_trending_repos(since_days=since_days, count=count, language=language)
    except RateLimitError as e:
        print(f"[WARN] {e}", file=sys.stderr)
        repos = _load_cache()
        used_cache = True
    except ApiError as e:
        print(f"[ERROR] API error (status={e.status_code}): {e}", file=sys.stderr)
        repos = _load_cache()
        used_cache = True
    except NetworkError as e:
        print(f"[ERROR] Network error: {e}", file=sys.stderr)
        repos = _load_cache()
        used_cache = True
    except ParseError as e:
        print(f"[ERROR] Parse error: {e}", file=sys.stderr)
        repos = _load_cache()
        used_cache = True
    except InvalidResponseError as e:
        print(f"[ERROR] Invalid response: {e}", file=sys.stderr)
        repos = _load_cache()
        used_cache = True

    if not repos:
        print("[FATAL] No repos available (API failed + no cache).", file=sys.stderr)
        sys.exit(2)

    # ── Generate HTML ──
    try:
        html = generate_html(repos, since_days=since_days, language=language)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(html)
    except OSError as e:
        print(f"[FATAL] Cannot write HTML output: {e}", file=sys.stderr)
        sys.exit(2)

    status = "[cache]" if used_cache else "[live]"
    print(f"[OK] Sticky note written {status}: {output_path}")
    print(f"[OK] {len(repos)} repos displayed.")

    sys.exit(1 if used_cache else 0)


if __name__ == "__main__":
    main()
