"""Checks for the competitor scout routine (mission criteria; tests/test_scout_watch.py runs the offline ones too).

    python3 tests/scout_check.py digest|quiet|handoff|registered|outage

Every mode but `registered` builds its own throwaway git repository and a local fake GitHub API on a free
port, then runs the workflow's steps exactly as scripts/competitor-scout.workflow.json has them, so modes can
run at the same time and never reach the network. `registered` reads the workflow Preconquer has registered
on this machine ($PRECONQUER_REAL_HOME, else ~/.preconquer).
"""
import datetime
import json
import os
import subprocess
import sys
import tempfile
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "scout_watch.py"
DEFINITION = json.loads((ROOT / "scripts" / "competitor-scout.workflow.json").read_text(encoding="utf-8"))
SHELL = DEFINITION["steps"][0]["command"]
AGENT = DEFINITION["steps"][1]
sys.path.insert(0, str(ROOT / "scripts"))
import scout_watch  # noqa: E402

REPOS = list(scout_watch.WATCHED)


def ago(hours):
    t = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def commit(repo, sha, message, hours):
    return {"sha": sha * 5, "html_url": f"https://github.com/{repo}/commit/{sha * 5}",
            "commit": {"message": message, "committer": {"date": ago(hours)}}}


def news(repo, tag="n"):
    """A day of work in `repo`: a release, feature commits and commits the digest leaves out."""
    key = repo.split("/")[1][:4]
    return {
        "commits": [
            commit(repo, f"{tag}1{key}", f"feat(cli): {key} {tag} resume a stopped task", 2),
            commit(repo, f"{tag}2{key}", f"fix: {key} {tag} crash on empty input", 3),
            commit(repo, f"{tag}3{key}", f"Merge pull request #9 from x/y\n\nAdd {key} {tag} branch picker", 4),
            commit(repo, f"{tag}4{key}", f"docs: {key} {tag} readme", 5),
            commit(repo, f"{tag}0{key}", f"feat: {key} {tag} too old to count", 60),
        ],
        "releases": [
            {"tag_name": f"v1-{key}-{tag}", "name": f"{key} {tag} 1.0", "published_at": ago(6), "prerelease": False,
             "html_url": f"https://github.com/{repo}/releases/tag/v1-{key}-{tag}"},
            {"tag_name": f"v0-{key}", "name": f"{key} 0.9", "published_at": ago(80), "prerelease": False,
             "html_url": f"https://github.com/{repo}/releases/tag/v0-{key}"},
        ],
    }


class Fake:
    """A local GitHub API: data[repo] = {commits, releases}; repos in `down` answer 500."""

    def __init__(self, data):
        self.data, self.down = data, set()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                url = urllib.parse.urlparse(self.path)
                q = urllib.parse.parse_qs(url.query)
                parts = url.path.strip("/").split("/")
                repo, kind = "/".join(parts[1:3]), parts[3] if len(parts) > 3 else ""
                if repo in fake.down:
                    self.send_response(500), self.end_headers()
                    return
                d = fake.data.get(repo, {"commits": [], "releases": []})
                if kind == "commits":
                    since = q.get("since", [""])[0]
                    body = [c for c in d["commits"] if c["commit"]["committer"]["date"] >= since]
                    body = body if q.get("page", ["1"])[0] == "1" else []
                elif kind == "releases":
                    body = d["releases"]
                else:
                    self.send_response(404), self.end_headers()
                    return
                raw = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class Project:
    """A throwaway project folder whose work branch has (or lacks) the scout script."""

    def __init__(self, tmp, with_script=True):
        self.dir = Path(tmp) / "project"
        self.dir.mkdir()
        self.home = Path(tmp) / "pq-home"
        git = lambda *a: subprocess.run(["git", *a], cwd=self.dir, check=True, capture_output=True)
        git("init", "-q", "-b", "preconquer-gogoymh")
        if with_script:
            (self.dir / "scripts").mkdir()
            (self.dir / "scripts" / "scout_watch.py").write_bytes(SCRIPT.read_bytes())
            git("add", "-A")
        git("-c", "user.email=a@b.invalid", "-c", "user.name=a", "commit", "-q", "--allow-empty", "-m", "x")
        self.digest = self.dir / ".git" / "goa-scout" / "digest.md"

    def env(self, api):
        env = {k: v for k, v in os.environ.items() if k not in ("GITHUB_TOKEN", "GH_TOKEN", "PRECONQUER_WORK_BRANCH")}
        env.update(GOA_SCOUT_API=api, PRECONQUER_HOME=str(self.home))
        return env

    def step(self, api):
        """Step 1 of the workflow, as the routine runs it."""
        return subprocess.run(["/bin/sh", "-c", SHELL], cwd=self.dir, env=self.env(api), capture_output=True, text=True)

    def agent_would_run(self, api):
        """Step 2's unless check: status 0 skips the agent."""
        return subprocess.run(["/bin/sh", "-c", AGENT["unless"]], cwd=self.dir, env=self.env(api)).returncode != 0


def fail(msg):
    sys.exit(f"scout_check: {msg}")


def check_digest():
    fake = Fake({r: news(r) for r in REPOS})
    try:
        with tempfile.TemporaryDirectory(prefix="goa-scout.") as tmp:
            p = Project(tmp)
            res = p.step(fake.url)
            if res.returncode != 0 or not p.digest.exists():
                fail(f"step 1 wrote no digest (exit {res.returncode}): {res.stdout}{res.stderr}")
            text = p.digest.read_text(encoding="utf-8")
            for repo in REPOS:
                key = repo.split("/")[1][:4]
                for want in (f"## {repo}", f"https://github.com/{repo}/releases/tag/v1-{key}-n",
                             f"https://github.com/{repo}/commit/{'n1' + key}", f"Add {key} n branch picker"):
                    if want not in text:
                        fail(f"digest misses {want!r}")
                for unwanted in (f"{key} n crash", f"{key} n readme", f"{key} n too old", f"v0-{key}"):
                    if unwanted in text:
                        fail(f"digest lists {unwanted!r}, which is not a new feature or release")
            print(f"ok digest: {len(REPOS)} repos, releases and feature commits linked, fixes and docs left out")
    finally:
        fake.close()


def check_quiet():
    fake = Fake({})
    try:
        with tempfile.TemporaryDirectory(prefix="goa-scout.") as tmp:
            p = Project(tmp, with_script=False)
            res = p.step(fake.url)
            if res.returncode != 0 or "skipping" not in res.stdout:
                fail(f"a branch without the script should skip cleanly: {res.stdout}{res.stderr}")
            if p.agent_would_run(fake.url):
                fail("the agent would start on a branch without the script")
        with tempfile.TemporaryDirectory(prefix="goa-scout.") as tmp:
            p = Project(tmp)
            res = p.step(fake.url)
            if res.returncode != 0 or "nothing new" not in res.stdout:
                fail(f"a quiet day should end with status 0 and say nothing new: {res.stdout}{res.stderr}")
            if p.digest.exists() or p.agent_would_run(fake.url):
                fail("a quiet day left a digest, so the agent would start")
        print("ok quiet: no digest and no agent without news, or before the script reaches the branch")
    finally:
        fake.close()


def check_handoff():
    prompt = AGENT["prompt"]
    for want in ("goa-scout/digest.md", "issue_list", "issue_create", "at most 3", "rm \"$(git rev-parse --git-common-dir)/goa-scout/digest.md\""):
        if want not in prompt:
            fail(f"the agent step's prompt lacks {want!r}")
    if AGENT.get("kind") != "agent" or AGENT.get("engine") != "claude":
        fail("step 2 must be a claude agent step")
    data = {r: news(r) for r in REPOS}
    fake = Fake(data)
    try:
        with tempfile.TemporaryDirectory(prefix="goa-scout.") as tmp:
            p = Project(tmp)
            p.step(fake.url)
            if not p.agent_would_run(fake.url):
                fail("a digest is waiting but the agent step would be skipped")
            first = p.digest.read_text(encoding="utf-8")
            # Not reviewed yet: the next run keeps the old items and adds the newer ones.
            repo = REPOS[0]
            data[repo]["commits"].insert(0, commit(repo, "zz9", "feat: later live canvas", 1))
            p.step(fake.url)
            second = p.digest.read_text(encoding="utf-8")
            if "later live canvas" not in second or "branch picker" not in second:
                fail("an unreviewed digest should keep its items and add newer ones")
            # The agent reviewed it (deleted the digest): the next run lists only what came after.
            p.digest.unlink()
            data[repo]["commits"].insert(0, commit(repo, "yy8", "feat: even later voice mode", 0.5))
            p.step(fake.url)
            third = p.digest.read_text(encoding="utf-8") if p.digest.exists() else ""
            if "even later voice mode" not in third or "later live canvas" in third or "branch picker" in third:
                fail(f"after a review only newer items belong in the digest, got:\n{third}")
            if first == third:
                fail("the digest did not move after the review")
        print("ok handoff: the agent runs on a digest, opens at most 3 non-duplicate tickets, deletes it, and the next run starts after it")
    finally:
        fake.close()


def check_outage():
    data = {r: news(r) for r in REPOS}
    fake = Fake(data)
    broken = REPOS[1]
    fake.down.add(broken)
    try:
        with tempfile.TemporaryDirectory(prefix="goa-scout.") as tmp:
            p = Project(tmp)
            for run in (1, 2, 3):
                res = p.step(fake.url)
                text = p.digest.read_text(encoding="utf-8") if p.digest.exists() else ""
                if f"## {REPOS[0]}" not in text or f"## {broken}" in text or f"- {broken}:" not in text:
                    fail(f"run {run}: the other repos should be summed up and {broken} named as not read:\n{text}")
                want = 1 if run == 3 else 0
                if res.returncode != want:
                    fail(f"run {run}: exit {res.returncode}, want {want}: {res.stdout}{res.stderr}")
            fake.down.clear()
            res = p.step(fake.url)
            if res.returncode != 0 or f"## {broken}" not in p.digest.read_text(encoding="utf-8"):
                fail(f"once {broken} answers again the run should pass and include it: {res.stdout}")
        print(f"ok outage: {broken} down, the rest summed up; the third failure in a row fails the run")
    finally:
        fake.close()


def check_registered():
    home = Path(os.environ.get("PRECONQUER_REAL_HOME") or Path.home() / ".preconquer")
    found = []
    for f in home.glob("workflows/git-ontology-agent-*/*.json"):
        try:
            w = json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if isinstance(w, dict) and w.get("name") == DEFINITION["name"]:
            found.append(w)
    if len(found) != 1:
        fail(f"want one '{DEFINITION['name']}' workflow under {home}, found {len(found)}")
    wf = found[0]
    r = wf.get("routine") or {}
    if (r.get("kind"), r.get("hour"), r.get("minute")) != ("daily", 9, 0):
        fail(f"routine must be daily at 09:00, got {r!r}")
    keys = ("kind", "command", "engine", "effort", "unless", "prompt")
    have = [{k: s.get(k) for k in keys} for s in wf.get("steps") or []]
    want = [{k: s.get(k) for k in keys} for s in DEFINITION["steps"]]
    if have != want:
        fail("registered steps differ from scripts/competitor-scout.workflow.json")
    print(f"ok registered: workflow {wf.get('id')} runs daily at 09:00 with the definition's {len(have)} steps")


MODES = {"digest": check_digest, "quiet": check_quiet, "handoff": check_handoff, "outage": check_outage,
         "registered": check_registered}

if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in MODES:
        sys.exit("usage: python3 tests/scout_check.py " + "|".join(MODES))
    MODES[sys.argv[1]]()
