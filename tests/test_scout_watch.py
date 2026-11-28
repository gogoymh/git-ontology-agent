"""Offline tests for scripts/scout_watch.py and the 'Competitor scout' workflow's steps.

Each test runs one mode of tests/scout_check.py: its own throwaway repository and fake GitHub API.

    python3 -m unittest tests.test_scout_watch
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scout_check  # noqa: E402
import scout_watch  # noqa: E402  (scout_check put scripts/ on the path)


class ScoutWatchTest(unittest.TestCase):
    def test_digest(self):
        scout_check.check_digest()

    def test_quiet(self):
        scout_check.check_quiet()

    def test_handoff(self):
        scout_check.check_handoff()

    def test_outage(self):
        scout_check.check_outage()

    def test_feature_filter(self):
        for subject in ("feat: x", "feat(ios)!: x", "Add a picker", "Support OSC 7577"):
            self.assertTrue(scout_watch.is_feature(subject), subject)
        for subject in ("fix: x", "test(agents): x", "docs: x", "chore(deps): bump", "Revert \"feat: x\"", "Update README"):
            self.assertFalse(scout_watch.is_feature(subject), subject)

    def test_merge_headline(self):
        self.assertEqual(scout_watch.headline("Merge pull request #1 from a/b\n\nfeat: y"), "feat: y")


if __name__ == "__main__":
    unittest.main()
