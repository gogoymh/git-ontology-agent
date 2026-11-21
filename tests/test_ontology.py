import unittest

from goa.ontology import (ASSUMPTION, CONFLICT, CONSTRAINT, DECISION, FINDING, INVALID, PROGRESS, REQUEST,
                          SUPERSEDED, Claim, State, merge3)


def trip():
    return State([
        Claim("req.1", REQUEST, "plan a Busan trip", "req.1"),
        Claim("trip.city", ASSUMPTION, "Busan", "req.1"),
        Claim("plan.train", DECISION, "Tue 08:00 KTX", "req.1", depends_on=["trip.city"]),
        Claim("hotel.candidates", FINDING, "A, B, C", "req.1", depends_on=["trip.city", "constraint.*"]),
        Claim("plan.hotel", DECISION, "C", "req.1", depends_on=["hotel.candidates", "constraint.*"]),
        Claim("plan.total", DECISION, "600k", "req.1", depends_on=["plan.train", "plan.hotel"]),
    ])


class DependencyGraph(unittest.TestCase):
    def test_new_constraint_redoes_only_its_dependents(self):
        s = trip()
        s.put(Claim("constraint.budget", CONSTRAINT, "500k", "req.2"))
        self.assertEqual(s.invalidate(["constraint.budget"]), ["hotel.candidates", "plan.hotel", "plan.total"])
        self.assertEqual(s.get("plan.train").status, "confirmed")   # KTX does not depend on the budget

    def test_correction_retracts_and_invalidates_everything_downstream(self):
        s = trip()
        s.put(Claim("constraint.budget", CONSTRAINT, "500k", "req.1"))
        old, inv = s.correct(Claim("trip.city", ASSUMPTION, "Daegu", "req.2"))
        self.assertEqual(old.value, "Busan")
        self.assertEqual(set(inv), {"plan.train", "hotel.candidates", "plan.hotel", "plan.total"})
        self.assertEqual(s.get("constraint.budget").status, "confirmed")  # budget is city independent
        self.assertEqual(s.get("trip.city#req.1").status, SUPERSEDED)
        self.assertEqual((s.get("trip.city").prev, s.get("trip.city").corrects), ("Busan", "req.1"))

    def test_yaml_round_trip(self):
        s = trip()
        self.assertEqual(State.from_yaml(s.to_yaml()).to_yaml(), s.to_yaml())
        self.assertTrue(s.to_yaml().splitlines()[1].startswith('- {"id": "req.1"'))


def three(cid, typ, base, ours, theirs, **kw):
    mk = lambda v, frm, at: State([Claim(cid, typ, v, frm, at=at, **kw)]) if v is not None else State()  # noqa
    return mk(base, "req.1", 1), mk(ours, "req.2", 2), mk(theirs, "req.3", 3)


class MergeRules(unittest.TestCase):
    def test_one_side_change_wins_without_rules(self):
        b, _, t = three("x.v", DECISION, "a", "a", "b")
        o = b.copy()
        merged, conflicts = merge3(b, o, t)
        self.assertEqual((merged.get("x.v").value, conflicts), ("b", []))

    def test_constraints_stack(self):
        b, o, t = three("constraint.depart", CONSTRAINT, None, "morning", "before 9")
        merged, _ = merge3(b, o, t)
        self.assertEqual(merged.get("constraint.depart").value, ["morning", "before 9"])

    def test_assumption_correction_wins(self):
        b = State([Claim("trip.city", ASSUMPTION, "Busan", "req.1", at=1)])
        o = State([Claim("trip.city", ASSUMPTION, "Seoul", "req.2", at=5)])
        t = State([Claim("trip.city", ASSUMPTION, "Daegu", "req.3", at=2, corrects="req.1")])
        merged, conflicts = merge3(b, o, t)
        self.assertEqual(merged.get("trip.city").value, "Daegu")
        self.assertEqual(conflicts, [])

    def test_later_finding_wins(self):
        b, o, t = three("seat.ktx", FINDING, "?", "available", "sold out")
        self.assertEqual(merge3(b, o, t)[0].get("seat.ktx").value, "sold out")

    def test_progress_is_never_merged(self):
        b, o, t = three("progress.step", PROGRESS, None, None, "picking hotel")
        self.assertIsNone(merge3(b, o, t)[0].get("progress.step"))

    def test_requests_are_all_kept(self):
        b = State()
        o = State([Claim("req.3", REQUEST, "meeting?", "req.3")])
        t = State([Claim("req.1", REQUEST, "trip", "req.1"), Claim("req.2", REQUEST, "budget", "req.2")])
        self.assertEqual(sorted(c.id for c in merge3(b, o, t)[0]), ["req.1", "req.2", "req.3"])

    def test_independent_conflict_uses_noul_judge(self):
        b, o, t = three("plan.hotel", DECISION, "A", "B", "C")
        merged, conflicts = merge3(b, o, t, judge=lambda e, l: 0.9)
        self.assertEqual((merged.get("plan.hotel").value, conflicts[0].resolved), ("C", "theirs"))

    def test_unresolved_conflict_keeps_both_values_for_the_final_answer(self):
        b, o, t = three("plan.hotel", DECISION, "A", "B", "C")
        merged, conflicts = merge3(b, o, t, judge=lambda e, l: 0.3)
        c = merged.get("plan.hotel")
        self.assertEqual((c.status, c.alternatives, conflicts[0].resolved), (CONFLICT, ["B", "C"], ""))

    def test_invalid_decision_never_wins(self):
        b, o, t = three("plan.hotel", DECISION, "A", "B", "C")
        o.get("plan.hotel").status = INVALID
        self.assertEqual(merge3(b, o, t)[0].get("plan.hotel").value, "C")


if __name__ == "__main__":
    unittest.main()
