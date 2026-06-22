"""GitHubResearchLoop — mine code & issues from GitHub for the brain.

Uses the GitHub Search API (free; 10/min unauth, 30/min with a PAT).
Picks a query from recent brain entries (or a fallback list relevant
to robotics) and asks GitHub for:

    1. Top code matches (snippets + repo path)
    2. Top issue/PR matches (titles)

Results land in brain.md as `[github]` entries — single line each, with
the URL and a short snippet so the brain (or the LLM, when an API key
is set) can decide whether to dig deeper.

Auth:
- Set `GITHUB_TOKEN=...` for 30 req/min and access to private repos
  you own. Free PAT, no cost.
- Without a token, falls back to anonymous (10 req/min). Still works,
  just slower.

Score:
    items_added (capped at 6 per cycle).
"""

from __future__ import annotations

import json
import os
import random
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from dimos.utils.logging_config import setup_logger

from ..loop_base import Loop

logger = setup_logger()

BRAIN_PATH = Path.home() / ".ohmni" / "brain.md"
GH_API = "https://api.github.com"

_FALLBACK_QUERIES = [
    "rplidar a2m8 python",
    "differential drive odometry python",
    "frontier exploration mapping",
    "ohmni telepresence",
    "voxel slam 2d lidar",
    "android adb tcp forward",
    "wavefront frontier exploration",
    "open3d voxel grid",
    "diff drive kinematics",
    "lidar express scan",
]


_STOPWORDS = {
    "the", "a", "an", "is", "are", "to", "of", "and", "or", "for",
    "on", "in", "with", "by", "robot", "system", "device", "platform",
    "software", "how", "to", "use", "do", "what",
}


def _sanitize_query(q: str) -> str:
    """Lowercase, strip punctuation, drop common stopwords. GitHub
    search tokenizer is picky — single common terms return 0, but a
    2-3 distinctive-word phrase usually returns hits."""
    q = re.sub(r"[^\w\s\-]", " ", q.lower())
    tokens = [t for t in q.split() if t and t not in _STOPWORDS]
    # Cap at 4 tokens to avoid over-narrowing
    return " ".join(tokens[:4])


def _gh_get(path: str, params: dict[str, str], timeout: float = 15.0) -> dict | None:
    url = f"{GH_API}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "ohmni-autoresearch",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
        return json.loads(data)
    except Exception as e:  # noqa: BLE001
        logger.warning("github_research GET %s failed: %s", path, e)
        return None


def _pick_query() -> str:
    if not BRAIN_PATH.exists():
        return random.choice(_FALLBACK_QUERIES)
    try:
        lines = BRAIN_PATH.read_text().splitlines()[-300:]
    except OSError:
        return random.choice(_FALLBACK_QUERIES)
    candidates: list[str] = []
    pattern = re.compile(r"\b(how to|look up|research|investigate|driver|protocol|encoder|odom|frontier|imminent|stuck|scan|calibration)\b", re.IGNORECASE)
    for line in lines:
        s = line.strip()
        if not s or "[boot]" in s:
            continue
        if pattern.search(s):
            # extract a search-shaped phrase
            m = re.search(r"\b(?:[A-Za-z][\w-]+\s+){2,5}", s)
            if m:
                phrase = m.group(0).strip()
                if 8 <= len(phrase) <= 80:
                    candidates.append(phrase)
    if candidates:
        return random.choice(candidates[-12:])
    return random.choice(_FALLBACK_QUERIES)


def _append_brain(line: str, *, kind: str = "github") -> None:
    BRAIN_PATH.parent.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with BRAIN_PATH.open("a") as f:
        f.write(f"- {ts} [{kind}] {line}\n")


class GitHubResearchLoop(Loop):
    name = "github_research"
    budget_s = 8.0

    def __init__(self, *args, max_items: int = 6, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.max_items = max_items

    def propose(self) -> dict[str, Any]:
        raw = _pick_query()
        clean = _sanitize_query(raw) or random.choice(_FALLBACK_QUERIES)
        return {"knob": f"q={clean[:60]}", "query": clean, "notes": f"raw: {raw[:80]}"}

    def apply(self, proposal: dict[str, Any]) -> Any:
        return None  # additive; no rollback

    def run(self, proposal: dict[str, Any], budget_s: float) -> dict[str, Any]:
        query = proposal["query"]
        authed = bool(os.environ.get("GITHUB_TOKEN"))
        added = 0

        # 0. Pluck GitHub URLs already in brain.md (placed there by
        #    web_research) and fetch their README text. This bypasses
        #    GitHub's flaky unauth search and produces real signal even
        #    when /search returns empty.
        added += self._fetch_brain_repos(self.max_items)
        if added >= self.max_items:
            return {"added": added, "auth": authed, "via": "brain_repos"}

        # 1. Repo search — works without auth, ranks by stars
        repos = _gh_get(
            "/search/repositories",
            {"q": query, "per_page": "3", "sort": "stars"},
        )
        if repos:
            for item in repos.get("items", []):
                full = item.get("full_name", "?")
                desc = (item.get("description") or "")[:140]
                url = item.get("html_url", "")
                stars = item.get("stargazers_count", 0)
                line = (
                    f'q="{query[:50]}" repo: {full} (★{stars}) :: {desc} <{url}>'
                )
                _append_brain(line)
                added += 1
                if added >= self.max_items:
                    return {"added": added, "auth": authed}

        # 2. Issues / PRs — works without auth too
        issues = _gh_get(
            "/search/issues",
            {"q": query, "per_page": str(min(3, self.max_items - added))},
        )
        if issues:
            for item in issues.get("items", []):
                title = item.get("title", "")
                repo = item.get("repository_url", "").rsplit("/", 2)[-2:]
                repo_name = "/".join(repo) if len(repo) == 2 else "?"
                url = item.get("html_url", "")
                state = item.get("state", "?")
                line = (
                    f'q="{query[:50]}" issue/{state}: {repo_name} :: '
                    f'{title[:90]} <{url}>'
                )
                _append_brain(line)
                added += 1
                if added >= self.max_items:
                    return {"added": added, "auth": authed}

        # 3. Code search — only with auth (GitHub requires it for /search/code)
        if authed and added < self.max_items:
            code = _gh_get(
                "/search/code",
                {"q": query, "per_page": str(min(3, self.max_items - added))},
            )
            if code:
                for item in code.get("items", []):
                    repo = item.get("repository", {}).get("full_name", "?")
                    path = item.get("path", "?")
                    url = item.get("html_url", "")
                    line = f'q="{query[:50]}" code: {repo}/{path} <{url}>'
                    _append_brain(line)
                    added += 1
                    if added >= self.max_items:
                        break

        return {"added": added, "auth": authed}

    def score(self, observations: dict[str, Any]) -> float:
        return float(observations.get("added", 0))

    # -- helpers --

    def _fetch_brain_repos(self, max_items: int) -> int:
        """Find github.com URLs in brain.md, extract owner/repo, GET
        the repo metadata + README text, append a single-line digest
        per repo to brain.md as `[github-readme]`.

        We only do *new* repos — skip any already-digested in the past.
        """
        if not BRAIN_PATH.exists():
            return 0
        try:
            text = BRAIN_PATH.read_text()
        except OSError:
            return 0

        # Collect already-digested repo identifiers
        digested = set(re.findall(r"\[github-readme\][^<\n]*<https://github\.com/([^/>\s]+/[^/>\s]+)", text))
        # Collect candidate repos seen in any URL
        candidates: list[str] = []
        seen_local: set[str] = set()
        for m in re.finditer(r"https?://github\.com/([^/>\s\?\#]+)/([^/>\s\?\#]+)", text):
            owner, name = m.group(1), m.group(2)
            name = re.sub(r"\.git$", "", name)
            key = f"{owner}/{name}"
            if key in digested or key in seen_local:
                continue
            # Filter out obviously-non-repo paths
            if owner in {"orgs", "topics", "search", "users", "settings", "marketplace"}:
                continue
            seen_local.add(key)
            candidates.append(key)

        added = 0
        for key in candidates[: max_items]:
            owner, name = key.split("/", 1)
            meta = _gh_get(f"/repos/{owner}/{name}", {})
            if not meta or "full_name" not in meta:
                continue
            stars = meta.get("stargazers_count", 0)
            desc = (meta.get("description") or "")[:140]
            url = meta.get("html_url", f"https://github.com/{key}")

            # Try README (uses different endpoint that returns base64)
            readme = _gh_get(f"/repos/{owner}/{name}/readme", {})
            preview = ""
            if readme:
                import base64
                try:
                    body = base64.b64decode(readme.get("content", "")).decode(
                        "utf-8", errors="replace"
                    )
                    # First 200 chars of meaningful content (skip badges)
                    cleaned = re.sub(r"<[^>]+>", "", body)
                    cleaned = re.sub(r"\!\[[^\]]*\]\([^\)]*\)", "", cleaned)
                    cleaned = re.sub(r"\s+", " ", cleaned)
                    preview = cleaned.strip()[:240]
                except Exception:  # noqa: BLE001
                    pass

            line = (
                f"{key} (★{stars}) :: {desc} | "
                f"{preview}{' …' if preview else ''} <{url}>"
            )
            _append_brain(line, kind="github-readme")
            added += 1
            if added >= max_items:
                break
        return added
