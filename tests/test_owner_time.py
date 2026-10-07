"""Owner-stall-tolerant budgets: extended by owner stall time, at most twice, never past their limit."""
import unittest

from agent_desktop.owner_time import TOLERANCE, Budget, OwnerClock, expired, hard


class Clock:
    def __init__(self, now=100.0):
        self.now = now

    def __call__(self):
        return self.now


class OwnerClockTests(unittest.TestCase):
    def test_regular_turns_are_not_stalls(self):
        now = Clock()
        owner = OwnerClock(now)
        for _ in range(100):
            owner.turn()
            now.now += .005
        self.assertEqual(owner.stalled(now()), 0.0)

    def test_gaps_beyond_tolerance_and_the_current_turn_count(self):
        now = Clock()
        owner = OwnerClock(now)
        self.assertEqual(owner.stalled(now()), 0.0)  # No turn yet: nothing is known.
        owner.turn()
        now.now += .100
        owner.turn()
        self.assertAlmostEqual(owner.total, .100 - TOLERANCE)
        now.now += .050  # Still in that turn.
        self.assertAlmostEqual(owner.stalled(now()), .100 - TOLERANCE + .050 - TOLERANCE)


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.now = Clock()
        self.owner = OwnerClock(self.now)
        self.owner.turn()

    def budget(self, seconds, **kwargs):
        return Budget(seconds, clock=self.owner, now=self.now, **kwargs)

    def stall(self, seconds):
        self.now.now += seconds
        self.owner.turn()

    def test_without_a_clock_it_is_a_plain_deadline(self):
        budget = Budget(.5, now=self.now)
        self.assertEqual((budget.at(), budget.hard), (100.5, 100.5))
        self.now.now = 100.5
        self.assertTrue(budget.expired())

    def test_unstalled_owner_expires_on_time(self):
        budget = self.budget(.5)
        for _ in range(99):
            self.stall(.005)
        self.assertFalse(budget.expired())
        self.now.now = 100.5
        self.owner.turn()
        self.assertTrue(budget.expired())

    def test_owner_stall_moves_the_deadline_by_the_stall(self):
        budget = self.budget(.5)
        self.stall(.300)
        self.assertAlmostEqual(budget.at(), 100.5 + .300 - TOLERANCE)
        self.now.now = 100.75
        self.assertFalse(budget.expired())

    def test_extension_is_capped_at_the_budget_again(self):
        budget = self.budget(.5)
        self.stall(5)  # A stall longer than the budget: a hung peer still fails at twice the budget.
        self.assertEqual(budget.at(), 101.0)
        self.assertTrue(budget.expired())
        self.assertEqual(hard(budget), 101.0)

    def test_never_past_its_limit(self):
        budget = self.budget(.5, limit=100.2)
        self.stall(.300)
        self.assertEqual((budget.at(), budget.hard), (100.2, 100.2))

    def test_stall_before_the_budget_began_does_not_count(self):
        self.stall(.300)
        budget = self.budget(.5)
        self.assertEqual(budget.at(), 100.8)

    def test_record_wait_credit_moves_both_deadlines_but_never_past_the_limit(self):
        budget = self.budget(.5, limit=101.5)
        budget.credit(.3)  # Waiting for the toolkit's own durable record (#96).
        self.assertAlmostEqual(budget.at(), 100.8)
        self.assertAlmostEqual(budget.hard, 101.3)
        budget.credit(1)
        self.assertEqual((budget.at(), budget.hard), (101.5, 101.5))
        budget.credit(-1)
        self.assertEqual(budget.at(), 101.5)

    def test_helpers_accept_plain_deadlines(self):
        self.assertTrue(expired(100.0, 100.0))
        self.assertFalse(expired(100.1, 100.0))
        self.assertEqual(hard(100.1), 100.1)


if __name__ == '__main__':
    unittest.main()
