"""Shutdown drives actual scheduler cancellation before release/close hooks."""
import unittest
from test_scheduler import Clock, Admission, Task
from agent_desktop.scheduler import Scheduler
from agent_desktop.shutdown import Shutdown


class ShutdownTests(unittest.TestCase):
    def setup_owner(self):
        clock, events, tasks = Clock(), [], []
        def factory(request, context):
            task = Task(context, events, clock)
            tasks.append(task)
            return task
        owner = Scheduler(clock=clock, factory=factory, capabilities={'input.reset', 'session.stop'})
        active = Admission(clock)
        owner.submit(active.request, active)
        owner.tick()
        return clock, events, tasks, owner, active

    def test_cancel_release_close_order_and_ordinary_rejection(self):
        clock, events, tasks, owner, active = self.setup_owner()
        def hook(stage):
            return lambda now, deadline: events.append((stage, now)) or {'state': 'attempted'}
        shutdown = Shutdown(owner, 1.8, clock=clock, release=hook('release'), close=hook('close'))
        active.disconnect()
        shutdown.tick()
        self.assertTrue(shutdown.done)
        self.assertEqual([event[0] for event in events], ['step', 'cancel', 'cleanup', 'release', 'close'])
        rejected = Admission(clock)
        owner.submit(rejected.request, rejected)
        self.assertEqual(rejected.results[0]['error'].code, 'session_unavailable')

    def test_sixteen_second_active_cleanup_cannot_consume_later_stages(self):
        clock, events, tasks, owner, active = self.setup_owner()
        tasks[0].cleanup_seconds = 16
        tasks[0].clean = False
        shutdown = Shutdown(owner, 1.8, clock=clock)
        shutdown.tick()
        self.assertFalse(shutdown.done)
        clock.now = .5
        shutdown.tick()
        self.assertTrue(shutdown.done)
        self.assertEqual(shutdown.results['release']['state'], 'not_connected')
        self.assertEqual(shutdown.results['close']['replacement_issue'], 35)

    def test_raising_and_late_release_still_attempt_close_within_original_end(self):
        for mode in ('raise', 'late'):
            clock, events, tasks, owner, active = self.setup_owner()
            def release(now, deadline):
                if mode == 'raise':
                    raise RuntimeError('fixture')
                clock.now = .8
                return None
            shutdown = Shutdown(owner, 1.8, clock=clock, release=release,
                                close=lambda now, deadline: events.append(('close', now)) or {'state': 'attempted'})
            shutdown.tick()
            self.assertTrue(shutdown.done)
            self.assertEqual(events[-1][0], 'close')
            self.assertEqual(shutdown.deadline, 1.8)

    def test_expired_callback_cannot_claim_later_close_was_attempted(self):
        clock, events, tasks, owner, active = self.setup_owner()
        def release(now, deadline):
            clock.now = 2
            return None
        shutdown = Shutdown(owner, 1.8, clock=clock, release=release,
                            close=lambda now, deadline: self.fail('late close'))
        shutdown.tick()
        self.assertTrue(shutdown.done)
        self.assertFalse(shutdown.results['close']['confirmed'])

    def test_superseded_reset_cancellation_is_bounded_by_shutdown(self):
        clock, events, tasks, owner, active = self.setup_owner()
        reset = Admission(clock, 'input.reset')
        owner.submit(reset.request, reset)
        owner.tick()
        tasks[-1].clean = False
        tasks[-1].cleanup_seconds = 16
        shutdown = Shutdown(owner, 1.8, clock=clock)
        shutdown.tick()
        clock.now = .5
        shutdown.tick()
        self.assertTrue(shutdown.done)
