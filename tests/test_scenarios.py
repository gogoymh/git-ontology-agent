"""The spec's scenarios, end to end, on real git repositories (offline LLM, local decision model)."""
import shutil
import tempfile
import unittest

from goa import Agent
from goa.decision import DecisionModel


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="goa-test-")
        self.agent = Agent(self.dir, lang="en")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def git(self, *a):
        return self.agent.repo.git(*a)

    def ticks(self, n):
        return [a for _ in range(n) for a in self.agent.tick()]


class BudgetAndIndependent(Base):
    """R1 trip, R2 budget (k1 supplement), R3 meeting (k3 independent)."""

    def test_flow(self):
        a = self.agent
        self.assertEqual(a.submit("Plan my Busan business trip next week"), [])
        self.ticks(2)                                    # KTX, then 3 hotel candidates
        [i1] = a.submit("Oh, keep the budget under 500,000 won")
        self.assertEqual((i1.kind, i1.relation), ("interim", "k1"))
        self.assertIn("KTX is set", i1.text)
        self.assertIn("redoing the hotel candidates", i1.text)
        self.assertNotIn("Hotel C", i1.text)             # invalid candidates are never said
        self.assertTrue(i1.source.startswith("req/1@"))

        # only budget dependents were invalidated; the KTX decision stays
        st = a.repo.read_state("req/1")
        self.assertEqual(st.get("plan.train").status, "confirmed")
        self.assertEqual(st.get("hotel.candidates").status, "invalid")

        self.assertEqual(a.submit("By the way, what time is tomorrow's meeting?"), [])
        out = a.run()
        i2, final = out
        self.assertEqual((i2.kind, i2.relation), ("interim", "k3"))
        self.assertIn("Tomorrow's meeting is at 2:00 PM", i2.text)
        self.assertIn("Still working on the Busan trip plan", i2.text)
        self.assertEqual(final.kind, "final")
        self.assertIn("Hotel A (2 nights, ₩180,000)", final.text)
        self.assertIn("total ₩460,000 (within the ₩500,000 budget)", final.text)
        self.assertIn("came from your second request", final.text)
        self.assertNotIn("Tomorrow", final.text)         # independent answer is not repeated

    def test_git_history(self):
        a = self.agent
        a.submit("Plan my Busan business trip next week"); self.ticks(2)
        a.submit("Oh, keep the budget under 500,000 won")
        a.submit("By the way, what time is tomorrow's meeting?"); a.run()
        # two real merge commits on main: req/3 first, then req/1
        merges = self.git("log", "--merges", "--format=%s", "main").splitlines()
        self.assertEqual(merges, ["Merge req/1: Busan trip plan", "Merge req/3: tomorrow's meeting"])
        parents = self.git("rev-list", "--parents", "-n", "1", "main").split()
        self.assertEqual(len(parents), 3)
        self.assertEqual(parents[2], self.git("rev-parse", "req/1"))
        # the budget commit carries the spec trailers
        body = self.git("log", "--format=%B", "-n", "1", "--grep", "constraint.budget: add", "req/1")
        for t in ("Request: req.2", "Relation: k1 supplement (confidence 0.94)", "Branch: req/1", "Base: ",
                  "Invalidates: hotel.candidates", "Decision-Model: local/heuristic"):
            self.assertIn(t, body)
        # main holds every claim with provenance; Progress never reaches main
        main = a.repo.read_state("main")
        self.assertEqual(main.get("constraint.budget").from_, "req.2")
        self.assertEqual(main.get("meeting.tomorrow").from_, "req.3")
        self.assertEqual(sorted(c.id for c in main.of("Request")), ["req.1", "req.2", "req.3"])
        # answers are commits that say what they were written from
        answers = self.git("log", "--format=%B", "answers")
        self.assertEqual(answers.count("Answer: interim"), 2)
        self.assertEqual(answers.count("Answer: final"), 1)
        self.assertRegex(answers, r"Source: main@[0-9a-f]{7}")
        self.assertIn("state.yaml", self.git("ls-tree", "--name-only", "main"))
        self.assertIn("policy.yaml", self.git("ls-tree", "--name-only", "main"))


class Correction(Base):
    def test_city_correction(self):
        a = self.agent
        a.submit("Plan my Busan business trip next week"); self.ticks(2)
        [i] = a.submit("Actually, not Busan but Daegu")
        self.assertEqual(i.relation, "k2")
        self.assertIn("Changed the trip city to Daegu", i.text)
        self.assertIn("Dropping the KTX and hotel candidates found for Busan", i.text)
        st = a.repo.read_state("req/1")
        self.assertEqual(st.get("trip.city#req.1").status, "superseded")
        self.assertEqual({c.id for c in st if c.status == "invalid"}, {"plan.train", "hotel.candidates"})
        [final] = a.run()
        self.assertIn("Tue 08:30 KTX (Seoul→Dongdaegu)", final.text)
        self.assertIn("Hotel B (2 nights, ₩160,000)", final.text)
        self.assertIn("The trip city was changed to Daegu in your second request", final.text)
        self.assertNotIn("Busan)", final.text)
        self.assertIn("trip.city: correct (Busan → Daegu)", self.git("log", "--format=%s", "req/1"))


class ProgressAndCancel(Base):
    def test_progress_then_cancel(self):
        a = self.agent
        a.submit("Plan my Busan business trip next week"); self.ticks(2)
        before = self.git("rev-parse", "req/1")
        [p] = a.submit("How far along are you?")
        self.assertEqual(p.relation, "k5")
        self.assertIn("Found hotel candidates: Hotel C, Hotel B, Hotel A", p.text)
        self.assertIn("Now working on the hotel and total", p.text)
        self.assertEqual(self.git("rev-parse", "req/1"), before)     # progress changes nothing
        [c] = a.submit("The trip got cancelled, you can stop")
        self.assertEqual(c.relation, "k4")
        self.assertIn("Stopped the Busan trip plan", c.text)
        self.assertEqual(a.run(), [])                                 # nothing to merge: no final answer
        self.assertEqual(a.repo.branches("req/"), [])
        self.assertEqual(a.repo.branches("cancelled/"), ["cancelled/req/1"])
        self.assertEqual(self.git("log", "--merges", "--format=%s", "main"), "")


class Korean(Base):
    def test_korean_answers(self):
        a = Agent(tempfile.mkdtemp(prefix="goa-test-"), lang="ko")
        a.submit("다음 주 부산 출장 일정 짜줘"); a.tick(); a.tick()
        [i1] = a.submit("아 예산은 50만원 이하로")
        self.assertIn("예산 50만원 조건을 반영해", i1.text)
        a.submit("근데 내일 회의 몇 시였지?")
        i2, final = a.run()
        self.assertIn("내일 회의는 오후 2시입니다", i2.text)
        self.assertIn("합계 46만원(예산 50만원 안)", final.text)
        self.assertIn("예산 조건은 두 번째 요청에서 반영했습니다", final.text)
        shutil.rmtree(a.repo.path, ignore_errors=True)


class Unsure(DecisionModel):
    name = "test/unsure"

    def _decide(self, state, questions):
        if "clarify" in questions:
            return {"clarify": {"choice": "k1", "confidence": 0.9}}
        return {"relation": {"choice": "k1", "confidence": 0.5}}


class Clarify(Base):
    def test_low_confidence_asks_then_applies(self):
        a = Agent(self.dir + "-u", lang="en", decision=Unsure())
        a.submit("Plan my Busan business trip next week"); a.tick()
        [q] = a.submit("Oh, keep the budget under 500,000 won")
        self.assertEqual(q.text, "Should I apply this to the Busan trip plan in progress, or handle it separately?")
        [i] = a.submit("yes, apply it")
        self.assertEqual(i.relation, "k1")
        self.assertEqual(a.repo.read_state("req/1").get("constraint.budget").from_, "req.2")
        shutil.rmtree(a.repo.path, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
