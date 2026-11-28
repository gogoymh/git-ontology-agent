#!/usr/bin/env python3
"""Watch competitor repositories and write a digest of what they shipped since the last review.

Meant to run from the Preconquer workflow 'Competitor scout' in the project folder, whatever branch
that folder has checked out:

  git show preconquer-gogoymh:scripts/scout_watch.py | python3 -

What one run does:
  1. Reads the state folder (<git common dir>/goa-scout by default). When the last digest.md is gone,
     the scout agent reviewed it: the cursors move to where that digest ended.
  2. Asks the GitHub API, per watched repository, for releases and commits after its cursor
     (one day back on the first run). Only commits whose subject reads as a feature count:
     fixes, tests, docs, chores, refactors and dependency bumps are left out.
  3. Something new: writes digest.md with a link for every release and feature commit, which the
     workflow's agent step turns into suggested tickets and then deletes. Nothing new: no digest.md,
     the cursors move on, and the agent step is skipped.
  4. A repository that cannot be read keeps its cursor and the others go on. The third failure in a
     row of the same repository ends the run with status 1, so the routine shows as failed.

Exit status: 0 when the run finished (digest.md is the signal), 1 when a repository failed three
runs in a row, 2 on a usage or state error.
Every line said also goes to the Preconquer log ($PRECONQUER_HOME, else ~/.preconquer, logs/preconquer.log).
Environment: GITHUB_TOKEN or GH_TOKEN (else `gh auth token`) raises the API rate limit; GOA_SCOUT_API
points at another API root (tests use a local fake).
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# The repositories watched, as owner/name. 'oh-my-code' names no single repository: the three oh-my
# harnesses for Claude Code, Codex and OpenCode are watched together.
WATCHED = (
    "openclaw/openclaw",
    "NousResearch/hermes-agent",
    "Yeachan-Heo/oh-my-claudecode",
    "Yeachan-Heo/oh-my-codex",
    "code-yeongyu/oh-my-openagent",
)
FAIL_LIMIT = 3
MOST_COMMITS = 25  # per repository in one digest; the rest is counted, not listed
MOST_RELEASES = 5
MOST_PAGES = 3  # of 100 commits per repository and run
API = "https://api.github.com"

FEATURE = re.compile(r"^(feat|feature)(\([^)]*\))?!?:\s*", re.I)
FEATURE_WORD = re.compile(r"^(add|adds|added|introduce|introduces|support|supports|new|enable|implement|allow)\b", re.I)
NOT_FEATURE = re.compile(
    r"^(fix|fixes|fixed|hotfix|bug|test|tests|docs?|chore|refactor|style|ci|build|perf|revert|deps?|bump|release|"
    r"merge branch|wip|lint|format|typo|update (dependency|deps))\b", re.I)


def log(msg: str) -> None:
    print(f"[scout] {msg}", flush=True)
    try:
        logs = Path(os.environ.get("PRECONQUER_HOME") or Path.home() / ".preconquer") / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(logs / "preconquer.log", "a", encoding="utf-8") as f:
            f.write(f"{stamp} {os.getpid()} scout: {msg}\n")
    except OSError:
        pass


def utc(text: str) -> datetime.datetime:
    return datetime.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)


def iso(moment: datetime.datetime) -> str:
    return moment.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def default_state() -> Path:
    res = subprocess.run(["git", "rev-parse", "--git-common-dir"], capture_output=True, text=True)
    if res.returncode != 0:
        raise SystemExit("[scout] not a git folder; pass --state")
    return Path(res.stdout.strip()).resolve() / "goa-scout"


def token() -> Optional[str]:
    for key in ("GITHUB_TOKEN", "GH_TOKEN"):
        if os.environ.get(key):
            return os.environ[key]
    if os.environ.get("GOA_SCOUT_API"):
        return None  # never hand the user's token to another API root
    try:
        res = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=10)
        if res.returncode != 0:
            return None
        return res.stdout.strip() or None
    except (OSError, subprocess.TimeoutExpired):
        return None


class Api:
    def __init__(self, root: str, auth: Optional[str]):
        self.root = root.rstrip("/")
        self.auth = auth

    def get(self, path: str, **query: Any) -> Any:
        url = f"{self.root}{path}" + ("?" + urllib.parse.urlencode(query) if query else "")
        req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "goa-scout"})
        if self.auth:
            req.add_header("Authorization", f"Bearer {self.auth}")
        with urllib.request.urlopen(req, timeout=30) as res:
            return json.loads(res.read().decode("utf-8"))


def headline(message: str) -> str:
    """The subject that says what a commit does; a merge commit's PR title is on its body's first line."""
    lines = [l.strip() for l in message.splitlines() if l.strip()]
    if not lines:
        return ""
    if lines[0].lower().startswith("merge pull request") and len(lines) > 1:
        return lines[1]
    return lines[0]


def is_feature(subject: str) -> bool:
    if FEATURE.match(subject):
        return True
    if NOT_FEATURE.match(subject) or re.match(r"^[a-z]+(\([^)]*\))?!?:", subject):
        return False  # any other conventional type (fix:, test(x):, ...) is not a feature
    return bool(FEATURE_WORD.match(subject))


def look(api: Api, repo: str, cursor: Dict[str, str]) -> Tuple[List[Dict], List[Dict], int, Dict[str, str]]:
    """Releases and feature commits after `cursor`; also the count of commits left out and the next cursor."""
    since = cursor["commits"]
    commits: List[Dict] = []
    for page in range(1, MOST_PAGES + 1):  # a busy repository makes hundreds of commits a day
        got = api.get(f"/repos/{repo}/commits", since=since, per_page=100, page=page)
        commits += got
        if len(got) < 100:
            break
    releases = api.get(f"/repos/{repo}/releases", per_page=20)
    nxt = dict(cursor)
    feats, skipped = [], 0
    for c in commits:
        when = c["commit"]["committer"]["date"]
        if when <= since:
            continue
        nxt["commits"] = max(nxt["commits"], when)
        subject = headline(c["commit"]["message"])
        if is_feature(subject):
            feats.append({"sha": c["sha"][:9], "date": when, "subject": subject, "url": c.get("html_url")
                          or f"https://github.com/{repo}/commit/{c['sha']}"})
        else:
            skipped += 1
    news = []
    for r in releases:
        when = r.get("published_at") or ""
        if r.get("draft") or not when or when <= cursor["releases"]:
            continue
        nxt["releases"] = max(nxt["releases"], when)
        news.append({"tag": r.get("tag_name", ""), "name": r.get("name") or r.get("tag_name", ""), "date": when,
                     "url": r.get("html_url", ""), "prerelease": bool(r.get("prerelease"))})
    news.sort(key=lambda r: r["date"], reverse=True)
    return news, feats, skipped, nxt


def render(found: Dict[str, Tuple[List[Dict], List[Dict], int]], failed: Dict[str, str], now: str) -> str:
    lines = [f"# Competitor scout digest ({now})", "",
             "New releases and feature commits since the last review. Each line links to its source.", ""]
    for repo, (releases, feats, skipped) in found.items():
        if not releases and not feats:
            continue
        lines.append(f"## {repo}")
        lines.append(f"https://github.com/{repo}")
        stable = [r for r in releases if not r["prerelease"]] or releases
        for r in stable[:MOST_RELEASES]:
            lines.append(f"- release {r['name']} ({r['date'][:10]}): {r['url']}")
        if len(releases) > len(stable[:MOST_RELEASES]):
            lines.append(f"- and {len(releases) - len(stable[:MOST_RELEASES])} more release(s), prereleases included")
        for c in feats[:MOST_COMMITS]:
            lines.append(f"- feat {c['sha']} ({c['date'][:10]}): {c['subject']} {c['url']}")
        if len(feats) > MOST_COMMITS:
            lines.append(f"- and {len(feats) - MOST_COMMITS} more feature commit(s)")
        lines.append(f"- {skipped} other commit(s) (fixes, tests, docs, chores) left out")
        lines.append("")
    if failed:
        lines.append("## Not read this run")
        lines += [f"- {repo}: {why}" for repo, why in failed.items()]
        lines.append("")
    return "\n".join(lines)


def run(state_dir: Path, repos: List[str], api: Api, since_days: float, now: datetime.datetime) -> int:
    state_dir.mkdir(parents=True, exist_ok=True)
    state_file, digest = state_dir / "state.json", state_dir / "digest.md"
    try:
        state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {}
    except ValueError as e:
        log(f"state file {state_file} is not JSON: {e}")
        return 2
    cursors: Dict[str, Dict[str, str]] = state.get("cursors", {})
    pending: Dict[str, Dict[str, str]] = state.get("pending", {})
    failures: Dict[str, int] = state.get("failures", {})
    if pending and not digest.exists():
        cursors.update(pending)
        log(f"last digest reviewed: cursors moved for {len(pending)} repo(s)")
        pending = {}
    elif digest.exists():
        log("last digest not reviewed yet: it is rebuilt from the same cursors with anything newer")
    start = iso(now - datetime.timedelta(days=since_days))
    found, failed, nxt_all = {}, {}, {}
    for repo in repos:
        cursor = cursors.get(repo) or {"commits": start, "releases": start}
        try:
            releases, feats, skipped, nxt = look(api, repo, cursor)
        except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError) as e:
            failures[repo] = failures.get(repo, 0) + 1
            why = getattr(e, "reason", None) or str(e)
            failed[repo] = f"{why} (failure {failures[repo]} in a row)"
            log(f"{repo}: could not read: {why}; failure {failures[repo]} in a row, cursor kept")
            continue
        failures.pop(repo, None)
        found[repo] = (releases, feats, skipped)
        nxt_all[repo] = nxt
        log(f"{repo}: {len(releases)} release(s), {len(feats)} feature commit(s), {skipped} other commit(s) since {cursor['commits']}")
    news = any(r or f for r, f, _ in found.values())
    if news:
        digest.write_text(render(found, failed, iso(now)), encoding="utf-8")
        pending = {**pending, **nxt_all}
        log(f"wrote {digest} for the scout agent")
    else:
        if digest.exists():
            digest.unlink()
        cursors.update(nxt_all)
        log("nothing new in the watched repositories")
    state = {"repos": repos, "cursors": cursors, "pending": pending, "failures": failures, "last_run": iso(now)}
    state_file.write_text(json.dumps(state, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    down = [r for r, n in failures.items() if n >= FAIL_LIMIT and r in repos]
    if down:
        log(f"FAILED: {', '.join(down)} could not be read {FAIL_LIMIT} runs in a row")
        return 1
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--state", type=Path, help="state folder (default <git common dir>/goa-scout)")
    p.add_argument("--repo", action="append", help="owner/name to watch (repeatable; default the WATCHED list)")
    p.add_argument("--since-days", type=float, default=1.0, help="how far back the first run looks (default 1)")
    p.add_argument("--now", help="the current time as YYYY-MM-DDTHH:MM:SSZ (tests)")
    a = p.parse_args(argv)
    now = utc(a.now) if a.now else datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    api = Api(os.environ.get("GOA_SCOUT_API") or API, token())
    return run(a.state or default_state(), a.repo or list(WATCHED), api, a.since_days, now)


if __name__ == "__main__":
    sys.exit(main())
