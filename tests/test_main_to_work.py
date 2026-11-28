"""scripts/main_to_work.sh carries external pull requests merged into main over to the work branch.

Each test builds its own throwaway remote and clone under a temp folder; nothing touches this repository.

    python3 -m unittest tests.test_main_to_work -q
"""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "main_to_work.sh"
WORK = "preconquer-gogoymh"
ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t.invalid", GIT_COMMITTER_NAME="t",
           GIT_COMMITTER_EMAIL="t@t.invalid", GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0")


class MainToWork(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="goa-m2w.")
        root = Path(self.tmp.name)
        self.env = dict(ENV, HOME=str(root), PRECONQUER_HOME=str(root / "pq"))
        self.remote, self.project, self.work_dir, self.ext = root / "remote.git", root / "project", root / "work", root / "ext"
        self.git(root, "init", "-q", "--bare", "-b", "main", str(self.remote))
        self.git(root, "clone", "-q", str(self.remote), str(self.project))
        self.commit(self.project, "app.py", "a = 1\nb = 2\nc = 3\n", "init")
        self.git(self.project, "push", "-q", "origin", "HEAD:main", f"HEAD:{WORK}")
        self.git(self.project, "fetch", "-q")
        self.git(self.project, "branch", "-q", WORK, f"origin/{WORK}")
        self.git(self.project, "worktree", "add", "-q", str(self.work_dir), WORK)
        self.git(root, "clone", "-q", str(self.remote), str(self.ext))

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, cwd, *args):
        return subprocess.run(["git", *args], cwd=cwd, env=self.env, check=True,
                              capture_output=True, text=True).stdout.strip()

    def commit(self, cwd, name, text, msg):
        (Path(cwd) / name).write_text(text)
        self.git(cwd, "add", name)
        self.git(cwd, "commit", "-q", "-m", msg)

    def external_pr(self, text, number=1):
        """Merges a pull request into main on the remote, the way GitHub does."""
        self.git(self.ext, "checkout", "-q", "main")
        self.git(self.ext, "pull", "-q", "origin", "main")
        self.git(self.ext, "checkout", "-q", "-b", f"pr{number}")
        self.commit(self.ext, "app.py", text, f"external change {number}")
        self.git(self.ext, "checkout", "-q", "main")
        self.git(self.ext, "merge", "-q", "--no-ff", f"pr{number}", "-m", f"Merge pull request #{number} from someone/pr{number}")
        self.git(self.ext, "push", "-q", "origin", "main")

    def run_sync(self):
        res = subprocess.run(["sh", str(SCRIPT)], cwd=self.project, env=dict(self.env, WORK_BRANCH=WORK),
                             capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        return res.stdout

    def remote_head(self, branch):
        return self.git(self.remote, "rev-parse", branch)

    def test_external_pr_reaches_work_branch(self):
        # A mission merged only into the local work branch, on another line of the same file.
        self.commit(self.work_dir, "app.py", "a = 1\nb = 2\nc = 30\n", "[GOA-1] Merge mission 1: local only")
        mission = self.git(self.work_dir, "rev-parse", "HEAD")
        self.external_pr("a = 10\nb = 2\nc = 3\n")
        out = self.run_sync()
        self.assertIn("pull requests #1", out)
        main, work = self.remote_head("main"), self.remote_head(WORK)
        for ancestor in (main, mission):
            self.git(self.remote, "merge-base", "--is-ancestor", ancestor, work)
        self.assertEqual(self.git(self.work_dir, "rev-parse", "HEAD"), work, "local work branch not moved")
        self.assertEqual((self.work_dir / "app.py").read_text(), "a = 10\nb = 2\nc = 30\n")
        self.assertIn("Merge pull request #1", self.git(self.work_dir, "log", "--format=%s"))
        self.assertIn("already holds", self.run_sync())

    def test_fast_forward_when_work_has_nothing_of_its_own(self):
        self.external_pr("a = 10\nb = 2\nc = 3\n", number=3)
        self.assertIn("fast-forward", self.run_sync())
        self.assertEqual(self.remote_head(WORK), self.remote_head("main"))
        self.assertEqual(self.git(self.work_dir, "rev-parse", "HEAD"), self.remote_head("main"))

    def test_clash_pushes_nothing(self):
        self.commit(self.work_dir, "app.py", "a = 2\nb = 2\nc = 3\n", "mission edits a")
        self.git(self.work_dir, "push", "-q", "origin", WORK)
        before = self.remote_head(WORK)
        self.external_pr("a = 99\nb = 2\nc = 3\n")
        out = self.run_sync()
        self.assertIn("clashes", out)
        self.assertIn("app.py", out)
        self.assertEqual(self.remote_head(WORK), before)
        self.assertEqual(self.git(self.work_dir, "rev-parse", "HEAD"), before)

    def test_logs_each_run(self):
        self.run_sync()
        log = Path(self.env["PRECONQUER_HOME"]) / "logs" / "preconquer.log"
        self.assertIn("main-to-work:", log.read_text())


if __name__ == "__main__":
    unittest.main()
