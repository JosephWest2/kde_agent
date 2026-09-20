"""Deterministic owner state machine and task contracts, without native imports."""
import sys
from pathlib import Path
import unittest
from unittest.mock import Mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_desktop.contracts import ContractError, make_request
from agent_desktop.scheduler import Scheduler
from agent_desktop.children import Children

GEN = "a" * 32
WINDOW = GEN + ":2a63a414-1509-460a-bff9-b7c1103ba8d5"


class Clock:
    now = 0
    def __call__(self):
        return self.now


class Admission:
    def __init__(self, clock, operation="session.status", timeout=3):
        args = {"window": WINDOW, "text": "hello"} if operation == "type" else {}
        self.request = make_request(operation, arguments=args, caller_cwd="/tmp",
                                    expected_generation=GEN, timeout_seconds=timeout)
        self.admitted_at = clock()
        self.deadline = clock() + timeout
        self.disconnected = False
        self.on_disconnect = None
        self.results = []
    def complete(self, **result):
        self.results.append(result)
    def disconnect(self):
        self.disconnected = True
        if self.on_disconnect:
            self.on_disconnect()


class Task:
    cleanup_seconds = .1
    def __init__(self, context, events, clock):
        self.context, self.events, self.clock = context, events, clock
        self.done = False
        self.clean = True
    def step(self, now):
        self.events.append(("step", self.context.work.request.request_id))
        self.context.effects({"app": {"generation": GEN, "application_id": "retained"}})
        return {"fixture": True} if self.done else None
    def request_cancel(self, reason):
        self.events.append(("cancel", self.context.work.request.request_id))
    def cleanup(self, now):
        self.events.append(("cleanup", self.context.work.request.request_id))
        return self.clean


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.clock, self.events, self.tasks = Clock(), [], []
        def factory(req, context):
            task = Task(context, self.events, self.clock)
            self.tasks.append(task)
            return task
        self.owner = Scheduler(clock=self.clock, factory=factory,
                               capabilities={"input.reset", "session.stop"})
    def submit(self, operation="session.status", timeout=3):
        a = Admission(self.clock, operation, timeout)
        self.owner.submit(a.request, a)
        return a

    def test_queue_deadline_and_single_owner(self):
        first = self.submit()
        self.owner.tick()
        second = self.submit(timeout=.05)
        self.clock.now = .05
        self.owner.tick()
        self.assertEqual(len(self.tasks), 1)
        self.assertEqual(second.results[0]["error"].code, "timeout")
        self.assertEqual(second.results[0]["error"].outcome, "not_started")
        self.assertEqual(self.owner.active.request, first.request)
        self.tasks[0].done = True
        third = self.submit()
        self.owner.tick()
        self.assertEqual(len(self.tasks), 2)
        self.assertEqual(len(first.results), 1)
        self.assertEqual(self.owner.active.request, third.request)

    def test_failed_session_cancels_work_distinctly_and_preserves_prior_cause(self):
        first = self.submit()
        self.owner.tick()
        queued = self.submit()
        self.owner.begin_shutdown(.5, failure=True)
        self.owner.drain_shutdown(.5)
        for admission in (first, queued):
            error = admission.results[0]['error']
            self.assertEqual(error.code, 'session_failed')
            self.assertIn('health failed', error.message)
        self.assertEqual(first.results[0]['error'].partial_result['app']['application_id'], 'retained')

    def test_failed_shutdown_does_not_replace_existing_cancel_or_timeout(self):
        first = self.submit()
        self.owner.tick()
        self.owner.cancel(first.request.request_id, code='timeout')
        self.owner.begin_shutdown(.5, failure=True)
        self.owner.drain_shutdown(.5)
        self.assertEqual(first.results[0]['error'].code, 'timeout')

    def test_disconnect_targets_only_matching_queued_or_active(self):
        first = self.submit()
        self.owner.tick()
        second = self.submit()
        second.disconnect()
        self.assertEqual(second.results[0]["error"].outcome, "not_started")
        self.assertFalse(any(e[0] == "cancel" for e in self.events))
        self.tasks[0].clean = False
        first.disconnect()
        self.owner.tick()
        self.assertIsNotNone(self.owner.active)
        self.assertEqual(len(first.results), 0)
        self.assertTrue(self.owner.cancel(first.request.request_id))
        self.tasks[0].clean = True
        self.owner.tick()
        self.assertEqual(first.results[0]["error"].outcome, "partial")
        self.assertEqual(first.results[0]["error"].partial_result["app"]["application_id"], "retained")
        self.assertFalse(self.owner.cancel(first.request.request_id))
        third = self.submit()
        first.disconnect()
        self.owner.tick()
        self.assertEqual(self.owner.active.request, third.request)

    def test_worker_timeout_and_cleanup_failure_fail_closed(self):
        first = self.submit(timeout=.05)
        second = self.submit()
        self.owner.tick()
        self.tasks[0].clean = False
        self.clock.now = .05
        self.owner.tick()
        self.assertEqual(self.events[-2][0], "cancel")
        self.clock.now = .16
        escalation = Mock()
        self.owner.escalate = escalation
        self.owner.tick()
        self.assertEqual(first.results[0]["error"].code, "timeout")
        self.assertEqual(first.results[0]["error"].outcome, "unknown")
        self.assertEqual(second.results[0]["error"].code, "session_unavailable")
        escalation.assert_called_once()
        self.assertEqual(len(self.tasks), 1)

    def test_reset_and_stop_bypass_and_survive_callers(self):
        first = self.submit("type")
        self.owner.tick()
        self.tasks[0].clean = False
        reset = self.submit("input.reset")
        reset.disconnect()
        self.owner.tick()
        self.assertEqual(len(self.tasks), 1)
        self.tasks[0].clean = True
        self.owner.tick()
        self.assertEqual(len(self.tasks), 2)
        self.assertEqual(first.results[0]["error"].code, "cancelled")
        stop = self.submit("session.stop")
        stop.disconnect()
        joined = self.submit("session.stop")
        self.owner.tick()
        self.owner.tick()
        self.assertEqual(len(self.tasks), 3)
        self.assertEqual(self.owner.lifecycle.request, stop.request)
        self.tasks[-1].done = True
        self.owner.tick()
        self.assertEqual(len(stop.results), 1)
        self.assertEqual(joined.results[0]["result"], {"fixture": True})
        self.assertTrue(self.owner.stopping)
        self.assertEqual(self.submit().results[0]["error"].code, "session_unavailable")

    def test_joined_waiter_cannot_extend_or_cancel_owner(self):
        first = self.submit("input.reset", timeout=1)
        self.owner.tick()
        joined = self.submit("input.reset", timeout=.01)
        self.clock.now = .02
        self.owner.tick()
        self.assertEqual(joined.results[0]["error"].code, "timeout")
        self.assertEqual(self.owner.lifecycle.admission.deadline, 1)
        self.assertFalse(self.owner.input_available)
        self.tasks[0].done = True
        self.owner.tick()
        self.assertIsNone(first.results[0]["error"])
        self.assertTrue(self.owner.input_available)

    def test_unsupported_controls_do_not_cancel_active(self):
        self.owner.capabilities = frozenset()
        first = self.submit()
        self.owner.tick()
        self.assertEqual(self.submit("session.stop").results[0]["error"].code, "unsupported_operation")
        self.assertEqual(self.owner.active.request, first.request)
        self.assertFalse(self.owner.stopping)

    def test_deadline_acceptance_after_step_and_observer(self):
        a = self.submit(timeout=.1)
        self.owner.tick()
        task = self.tasks[0]
        def late(now):
            self.clock.now = .1
            return {"app": {"generation": GEN, "application_id": "late"}}
        task.step = late
        self.owner.tick()
        self.owner.tick()
        self.assertEqual(a.results[0]["error"].code, "timeout")
        self.assertEqual(a.results[0]["error"].partial_result["app"]["application_id"], "late")
        b = self.submit(timeout=.1)
        self.owner.tick()
        self.tasks[-1].done = True
        def observer(event):
            if event["event"] == "finalizing":
                self.clock.now = b.deadline
        self.owner.observer = observer
        self.owner.tick()
        self.assertEqual(b.results[0]["error"].code, "timeout")

    def test_observer_failure_at_each_phase_cannot_skip_safety(self):
        for phase in ("admitted", "effects", "cancelling", "finalizing"):
            with self.subTest(phase=phase):
                self.setUp()
                def observer(event):
                    if event["event"] == phase:
                        if phase == "cancelling":
                            self.assertEqual(self.events[-1][0], "cancel")
                        raise OSError("storage unavailable")
                self.owner.observer = observer
                a = self.submit()
                self.owner.tick()
                if phase == "cancelling":
                    self.owner.cancel(a.request.request_id)
                if phase == "finalizing":
                    self.tasks[0].done = True
                self.owner.tick()
                self.owner.tick()
                self.assertEqual(len(a.results), 1)
                self.assertIsNotNone(a.results[0]["error"])
                self.assertFalse(self.owner.live)

    def test_observer_reentrancy_is_rejected(self):
        self.owner.observer = lambda e: self.owner.tick()
        a = self.submit()
        self.owner.tick()
        self.assertEqual(a.results[0]["error"].code, "artifact_failed")

    def test_factory_error_preserves_error_context_and_partial(self):
        def broken(req, ctx):
            raise ContractError("target_lost", "Target vanished.", context={"phase": "dequeue"},
                                outcome="partial", partial_result={"app": "retained"})
        self.owner.factory = broken
        a = self.submit()
        self.owner.tick()
        err = a.results[0]["error"]
        self.assertEqual((err.code, err.message, err.context, err.partial_result),
                         ("target_lost", "Target vanished.", {"phase": "dequeue"}, {"app": "retained"}))

    def test_lifecycle_storage_failure_does_not_skip_required_cleanup(self):
        self.owner.observer = lambda event: (_ for _ in ()).throw(OSError())
        stop = self.submit("session.stop")
        stop.disconnect()
        self.owner.tick()
        self.assertEqual(len(self.tasks), 1)
        self.tasks[0].done = True
        self.owner.tick()
        self.assertEqual(stop.results[0]["error"].code, "artifact_failed")
        self.assertTrue(self.owner.stopping)

    def test_stop_during_abort_uses_one_admission_budget(self):
        active = self.submit()
        self.owner.tick()
        self.tasks[0].cleanup_seconds = 1
        self.tasks[0].clean = False
        stop = self.submit("session.stop", timeout=.05)
        joined = self.submit("session.stop", timeout=1)
        short = self.submit("session.stop", timeout=.01)
        self.assertEqual(self.owner.active.cleanup_deadline, .05)
        self.clock.now = .05
        self.owner.tick()
        self.assertEqual(active.results[0]["error"].outcome, "unknown")
        self.assertEqual(stop.results[0]["error"].code, "timeout")
        self.assertEqual(joined.results[0]["error"].code, "timeout")
        self.assertEqual(len(self.tasks), 1)
        self.assertTrue(self.owner.unavailable)

    def test_factory_crossing_deadline_never_emits_and_retains_cleanup_owner(self):
        for construction_end in (.005, .010):
            with self.subTest(construction_end=construction_end):
                self.setUp()
                factory = self.owner.factory
                def slow_factory(request, context):
                    task = factory(request, context)
                    task.clean = False
                    self.clock.now = construction_end
                    return task
                self.owner.factory = slow_factory
                request = self.submit(timeout=.005)
                self.owner.tick()
                self.assertFalse(any(event[0] == "step" for event in self.events))
                self.assertEqual(self.events[0][0], "cancel")
                self.assertEqual(self.owner.active.request, request.request)
                self.assertFalse(request.results)
                self.tasks[0].clean = True
                self.owner.tick()
                self.assertEqual(request.results[0]["error"].code, "timeout")
                self.assertEqual(request.results[0]["error"].outcome, "not_started")

    def test_queued_expiry_observer_crossing_active_deadline_prevents_emission(self):
        for observer_end in (.010, .012):
            with self.subTest(observer_end=observer_end):
                self.setUp()
                active = self.submit(timeout=.010)
                self.owner.tick()
                self.tasks[0].clean = False
                queued = self.submit(timeout=.005)
                def observer(record):
                    if record["request_id"] == queued.request.request_id and record["event"] == "finalizing":
                        self.clock.now = observer_end
                self.owner.observer = observer
                self.clock.now = .006
                self.owner.tick()
                self.assertEqual(sum(event[0] == "step" for event in self.events), 1)
                self.assertEqual(self.owner.active.request, active.request)
                self.assertFalse(active.results)
                self.assertEqual(queued.results[0]["error"].code, "timeout")
                self.tasks[0].clean = True
                self.owner.tick()
                self.assertEqual(active.results[0]["error"].code, "timeout")
                self.assertEqual(active.results[0]["error"].partial_result["app"]["application_id"], "retained")

    def test_stop_deadline_includes_superseded_reset_cleanup_and_joiners(self):
        reset = self.submit("input.reset", timeout=3)
        reset_joined = self.submit("input.reset", timeout=2)
        self.owner.tick()
        self.tasks[0].clean = False
        self.tasks[0].cleanup_seconds = 2
        stop = self.submit("session.stop", timeout=.05)
        long_waiter = self.submit("session.stop", timeout=1)
        short_waiter = self.submit("session.stop", timeout=.01)
        stop.disconnect()
        long_waiter.disconnect()
        escalation = Mock()
        self.owner.escalate = escalation
        self.assertEqual(self.owner.lifecycle.cleanup_deadline, .05)
        self.assertEqual(self.owner.pending_stop.admission.deadline, .05)
        self.clock.now = .01
        self.owner.tick()
        self.assertEqual(short_waiter.results[0]["error"].code, "timeout")
        self.assertEqual(short_waiter.results[0]["error"].outcome, "not_started")
        self.assertFalse(stop.results)
        self.assertFalse(self.owner.unavailable)
        self.clock.now = .05
        self.owner.tick()
        self.assertTrue(self.owner.unavailable)
        self.assertFalse(self.owner.input_available)
        self.assertTrue(escalation.called)
        for waiter in (stop, long_waiter):
            self.assertEqual(waiter.results[0]["error"].code, "timeout")
            self.assertEqual(waiter.results[0]["error"].outcome, "unknown")
        for waiter in (reset, reset_joined):
            self.assertEqual(waiter.results[0]["error"].code, "cancelled")
            self.assertEqual(waiter.results[0]["error"].outcome, "unknown")
        self.assertEqual(len(self.tasks), 1, "No concurrent or late stop task")
        self.owner.tick()
        self.assertEqual(len(self.tasks), 1)
        self.assertEqual(len(stop.results), 1)
        self.assertEqual(len(long_waiter.results), 1)

    def test_automatic_abort_escalation_joins_one_shutdown_owner(self):
        for explicit in (False, True):
            with self.subTest(explicit=explicit):
                self.setUp()
                capture = self.submit("screenshot", timeout=.01)
                self.owner.tick()
                self.tasks[0].clean = False
                self.tasks[0].cleanup_seconds = .02
                explicit_stop = self.submit("session.stop", timeout=1) if explicit else None
                escalations = []
                def automatic_stop(reason):
                    # The future capture adapter routes its first unconfirmed
                    # abort through this seam; it does not open another owner.
                    if not escalations:
                        escalation = Admission(self.clock, "session.stop", 15)
                        escalations.append(escalation)
                        self.owner.submit(escalation.request, escalation)
                self.owner.escalate = automatic_stop
                self.clock.now = .01
                self.owner.tick()
                self.clock.now = .04
                self.owner.tick()
                self.assertEqual(len(escalations), 1)
                automatic = escalations[0]
                owner = self.owner.lifecycle
                self.assertEqual(owner.admission, explicit_stop or automatic)
                original_deadline = owner.admission.deadline
                self.clock.now = .05
                repeated = self.submit("session.stop", timeout=15)
                self.assertEqual(self.owner.lifecycle.admission.deadline, original_deadline)
                self.assertEqual(len(self.tasks), 2)
                self.tasks[-1].done = True
                self.owner.tick()
                self.assertIsNone(automatic.results[0]["error"])
                self.assertIsNone(repeated.results[0]["error"])
                self.assertEqual(capture.results[0]["error"].outcome, "unknown")
                self.assertEqual(len(self.tasks), 2, "Exactly one shutdown task")

    def test_child_reaper_survives_request_terminalization(self):
        children = Children()
        process = Mock()
        process.poll.side_effect = [None, None, None, 9]
        from agent_desktop.children import Child
        child = Child(process)
        children.owned.add(child)
        self.owner.children = children
        a = self.submit(timeout=.01)
        self.owner.tick()
        self.tasks[0].clean = False
        self.clock.now = .02
        child.abort()
        child.abort()
        self.owner.tick()
        self.clock.now = .2
        self.owner.tick()
        self.assertEqual(a.results[0]["error"].outcome, "unknown")
        self.assertTrue(children.owned)
        self.owner.tick()
        self.assertFalse(children.owned)
        process.kill.assert_called_once()

if __name__ == "__main__":
    unittest.main()
