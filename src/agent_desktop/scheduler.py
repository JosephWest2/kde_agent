"""Single-owner, bounded-step scheduling. No desktop bindings or native imports.

Task factory(request, context) returns an object with step(now) -> result|None,
request_cancel(reason), cleanup(now) -> bool and cleanup_seconds (<=16). Calls
must be bounded/nonblocking. Only this owner may call task methods. Context
records effects before cancellation can run; observers never receive argv/env.

An observer may return write tickets (objects with `done` and `error`; see
writer.py) for the records it queued. A work's first step waits until its
admission and start records are done (unless its task sets gates_effects and
waits with Context.recorded() before each effect itself), its response waits until every record it
queued is done, and a failed one latches observing_failed (no new effects,
artifact_failed). Tasks wait for their own effect records with
Context.recorded() before the effect they gate. Cancellation, release and
cleanup never wait for records.
"""
from __future__ import annotations

from collections import deque
from copy import deepcopy
from dataclasses import dataclass, field
import time

from .contracts import ContractError, dispatch

CONTROL_OPERATIONS = frozenset({"session.stop"})
MAX_ORDINARY = 32
# A response waits for its records at most this long past its deadline or cleanup
# deadline; then it is sent without them (never as a success). Matches writer.STALL_SECONDS.
SETTLE_GRACE = 5.0


class UnsupportedTask:
    cleanup_seconds = 0

    def __init__(self, request, context):
        self.request = request

    def step(self, now):
        dispatch(self.request)

    def request_cancel(self, reason):
        pass

    def cleanup(self, now):
        return True


@dataclass
class Work:
    admission: object
    task: object = None
    error: object = None
    outcome: str = "not_started"
    partial: object = None
    cleanup_deadline: float = 0
    terminal: bool = False
    observing_failed: bool = False
    result: object = None
    pending: list = field(default_factory=list)  # Write tickets not yet done.
    stepped: bool = False      # The first step ran (admission and start records were done).
    finishing: bool = False    # Outcome decided; the response waits for its records.
    settle_by: float = float("inf")

    @property
    def request(self):
        return self.admission.request


class Context:
    def __init__(self, owner, work):
        self.owner, self.work = owner, work

    def effects(self, partial_result=None, *, uncertain=False):
        self.owner._guard()
        self.work.outcome = "unknown" if uncertain else "partial"
        if partial_result is not None:
            self.work.partial = partial_result
        self.owner._observe(self.work, "effects")
        self.owner._refresh(self.work)
        if self.work.observing_failed and self.work.request.operation not in CONTROL_OPERATIONS:
            raise ContractError("artifact_failed", "Request record could not be preserved.")

    def recorded(self):
        """True once every record this request queued is durable. A task calls it after
        effects() and before the effect that record must precede; False means wait
        (return None and ask again on a later step). A failed record raises."""
        self.owner._refresh(self.work)
        if self.work.observing_failed and self.work.request.operation not in CONTROL_OPERATIONS:
            raise ContractError("artifact_failed", "Request record could not be preserved.")
        return not self.work.pending

    def track(self, tickets):
        """Count other durable writes (application records) as this request's records."""
        self.owner._track(self.work, tickets)


class Scheduler:
    def __init__(self, *, factory=UnsupportedTask, clock=time.monotonic,
                 capabilities=(), observer=None, escalate=None, children=None):
        self.factory, self.clock = factory, clock
        self.capabilities = frozenset(capabilities)
        self.observer = observer
        self.escalate = escalate or (lambda reason: None)
        self.children = children
        self.queue = deque()
        self.active = None
        self.live = {}
        self.lifecycle = None
        self.waiters = []
        self.pending_stop = None
        self.stopping = False
        self.unavailable = False
        self.finishing = []
        self._observing = False

    def _guard(self):
        if self._observing:
            raise RuntimeError("Observers cannot re-enter scheduler mutation")

    def _observe(self, work, event):
        if self.observer is None:
            return
        self._observing = True
        try:
            tickets = self.observer(deepcopy({"request_id": work.request.request_id,
                           "operation": work.request.operation,
                           "session": work.request.session,
                           "generation": work.request.expected_generation,
                           "event": event, "outcome": work.outcome,
                           "error_code": work.error.code if work.error else None,
                           "partial_result": work.partial}))
            self._track(work, tickets)
        except Exception:
            work.observing_failed = True
        finally:
            self._observing = False

    @staticmethod
    def _track(work, tickets):
        if tickets is None:
            return
        if hasattr(tickets, "done"):
            tickets = (tickets,)
        work.pending.extend(ticket for ticket in tickets if ticket is not None)

    @staticmethod
    def _refresh(work):
        """Drop finished tickets, latching any failure; True when none are left."""
        if work.pending:
            remaining = []
            for ticket in work.pending:
                if not ticket.done:
                    remaining.append(ticket)
                elif ticket.error is not None:
                    work.observing_failed = True
            work.pending = remaining
        return not work.pending

    def submit(self, request, admission):
        self._guard()
        work = Work(admission)
        terminal_observer = getattr(admission, "on_terminal", None)
        if terminal_observer is not None:
            def accepted(payload):
                self._observing = True
                try:
                    terminal_observer(deepcopy(payload))
                finally:
                    self._observing = False
            admission.on_terminal = accepted
        if request.operation in CONTROL_OPERATIONS:
            self._control(work)
            return
        if self.stopping or self.unavailable:
            admission.complete(error=ContractError("session_unavailable", "Session is unavailable."))
            return
        if len(self.live) >= MAX_ORDINARY:
            admission.complete(error=ContractError("session_unavailable", "Ordinary queue is full.",
                                                   context={"phase": "queue_full"}))
            return
        self.live[request.request_id] = work
        self.queue.append(work)
        admission.on_disconnect = lambda: self.cancel(request.request_id)
        self._observe(work, "admitted")
        if admission.disconnected:
            self.cancel(request.request_id)

    def cancel(self, request_id, *, code="cancelled"):
        self._guard()
        work = self.live.get(request_id)
        if work is None or work.terminal or work.finishing:
            return False
        self._cancel(work, code)
        return True

    def _cancel(self, work, code, error=None):
        if work.error is not None or work.finishing:
            return
        work.error = error or ContractError(code, "Request deadline expired." if code == "timeout" else "Request cancelled.")
        if work.task is None:
            if code == "timeout" and work.request.operation in CONTROL_OPERATIONS:
                self._fail_closed(work)
            self._finish(work)
            return
        # Stop emission / initiate release BEFORE diagnostics or persistence.
        try:
            work.task.request_cancel(code)
            seconds = float(work.task.cleanup_seconds)
            if not 0 <= seconds <= 16:
                raise ValueError
            work.cleanup_deadline = self.clock() + seconds
            if work.request.operation in CONTROL_OPERATIONS:
                work.cleanup_deadline = min(work.cleanup_deadline, work.admission.deadline)
        except Exception:
            work.outcome = "unknown"
            work.cleanup_deadline = self.clock()
        self._observe(work, "cancelling")

    def _fail_closed(self, work):
        self.unavailable = True
        work.outcome = "unknown"
        try:
            self.escalate("cleanup_unconfirmed")
        except Exception:
            pass

    def _finish(self, work, result=None):
        """Decide the outcome, record finalizing, and respond once its records are done."""
        if work.terminal or work.finishing:
            return
        if work.error is None and self.clock() >= work.admission.deadline:
            work.error = ContractError("timeout", "Request deadline expired.")
        self._observe(work, "finalizing")
        work.finishing = True
        work.result = result
        work.settle_by = min(work.settle_by, max(work.admission.deadline, work.cleanup_deadline) + SETTLE_GRACE)
        self.finishing.append(work)
        # The task's steps and cleanup are over: only the response waits for its
        # records, so the next work, a reset or stop, and shutdown's release
        # never wait for the writer (#96).
        if self.active is work:
            self.active = None
        self._settle(work)

    def settle(self):
        """Respond for every finishing work whose records are done (or out of time)."""
        for work in tuple(self.finishing):
            self._settle(work)

    def _settle(self, work):
        if work.terminal:
            return True
        result = work.result
        if not self._refresh(work):
            if self.clock() < work.settle_by:
                return False
            # Durability unknown: never answer success; the records stay pending.
            # Reported as artifact_failed, ahead of the deadline check (settle_by is
            # always past the deadline), so a storage stall is not called a timeout.
            work.observing_failed = True
            if work.error is None:
                work.error = ContractError("artifact_failed", "Request record could not be preserved.",
                                           context={"phase": "settle"})
                if isinstance(result, dict):
                    if work.partial is None:
                        work.outcome = "unknown"
                    work.partial = (work.partial or {}) | result
        if work.error is None and self.clock() >= work.admission.deadline:
            work.error = ContractError("timeout", "Request deadline expired.")
            if isinstance(result, dict):
                if work.partial is None:
                    work.outcome = "unknown"
                work.partial = (work.partial or {}) | result
        if work.observing_failed and work.error is None:
            work.error = ContractError("artifact_failed", "Request record could not be preserved.")
        error = work.error
        if error is not None:
            error = ContractError(error.code, error.message, context=error.context,
                                  outcome=work.outcome, partial_result=work.partial)
        work.terminal = True
        if work in self.finishing:
            self.finishing.remove(work)
        self.live.pop(work.request.request_id, None)
        work.result = result
        work.admission.complete(result=result, error=error)
        accepted = getattr(work.admission, "final_payload", None)
        if isinstance(accepted, dict) and not accepted["ok"]:
            final = accepted["error"]
            work.error = ContractError(final["code"], final["message"], context=final["context"],
                                       outcome=final["outcome"], partial_result=final["partial_result"])
            work.outcome, work.partial = final["outcome"], final["partial_result"]
        if self.active is work:
            self.active = None
        return True

    def _control(self, work):
        op = work.request.operation
        if op not in self.capabilities:
            work.admission.complete(error=ContractError("unsupported_operation", "Operation is not implemented yet."))
            return
        if self.clock() >= work.admission.deadline:
            work.admission.complete(error=ContractError("timeout", "Control deadline expired."))
            return
        existing = self.pending_stop if op == "session.stop" and self.pending_stop is not None else self.lifecycle
        joining = existing is not None and existing.request.operation == op
        if joining and len(self.waiters) >= 32:
            work.admission.complete(error=ContractError("session_unavailable", "Control waiters are full."))
            return
        owner_deadline = existing.admission.deadline if joining else work.admission.deadline
        if op == "session.stop":
            self.stopping = True
            for queued in tuple(self.queue):
                if not queued.terminal:
                    self._cancel(queued, "cancelled")
        if self.active is not None:
            self._cancel(self.active, "cancelled")
            if self.active is not None:
                self.active.cleanup_deadline = min(self.active.cleanup_deadline, owner_deadline)
        if self.lifecycle is None:
            self.lifecycle = work
        elif self.lifecycle.request.operation == op:
            if len(self.waiters) >= 32:
                work.admission.complete(error=ContractError("session_unavailable", "Control waiters are full."))
                return
            self.waiters.append(work)
        elif op == "session.stop":
            # One stop deadline is fixed on first stop admission, even while reset
            # cancellation or the active ordinary cleanup is still in progress.
            if self.pending_stop is None:
                self.pending_stop = work
                self._cancel(self.lifecycle, "cancelled")
                self.lifecycle.cleanup_deadline = min(self.lifecycle.cleanup_deadline, owner_deadline)
            else:
                self.waiters.append(work)
        else:
            work.admission.complete(error=ContractError("session_unavailable", "Shutdown is in progress."))
            return
        self._observe(work, "control_admitted")
        # No disconnect hook: required lifecycle cleanup belongs to the worker.

    def _advance(self, work):
        now = self.clock()
        if work.terminal:
            return
        if work.finishing:
            self._settle(work)
            return
        if work.error is None and now >= work.admission.deadline:
            self._cancel(work, "timeout")
        if work.terminal or work.finishing:
            return
        self._refresh(work)
        if work.observing_failed and work.error is None and work.request.operation not in CONTROL_OPERATIONS:
            self._cancel(work, "artifact_failed")
        if work.error is not None:
            try:
                clean = work.task is None or work.task.cleanup(self.clock())
            except Exception:
                clean = False
            if clean:
                self._finish(work)
            elif self.clock() >= work.cleanup_deadline:
                self._fail_closed(work)
                self._finish(work)
            return
        try:
            if work.task is None:
                work.task = self.factory(work.request, Context(self, work))
            if not work.stepped:
                # Construction is effect-free; the first step waits until the
                # admission and start records are durable, unless the task gates
                # each of its effects on Context.recorded() itself.
                # Controls are storage-independent and never wait here.
                if (not self._refresh(work) and not getattr(work.task, "gates_effects", False)
                        and work.request.operation not in CONTROL_OPERATIONS):
                    return
                if work.observing_failed and work.request.operation not in CONTROL_OPERATIONS:
                    self._cancel(work, "artifact_failed")
                    return
                work.stepped = True
            # Construction is effect-free but can still consume the remaining
            # budget. Never emit using time sampled before any callback.
            now = self.clock()
            if now >= work.admission.deadline:
                self._cancel(work, "timeout")
                return
            result = work.task.step(now)
            if result is not None:
                if self.clock() >= work.admission.deadline:
                    # Preserve task-recorded progress plus available late result.
                    if isinstance(result, dict):
                        if work.partial is None:
                            work.outcome = "unknown"
                        work.partial = (work.partial or {}) | result
                    self._cancel(work, "timeout")
                else:
                    self._finish(work, result)
        except ContractError as error:
            if error.partial_result is not None:
                work.partial = error.partial_result
            if error.outcome != "not_started":
                work.outcome = error.outcome
            self._cancel(work, error.code, error)
        except Exception:
            work.outcome = "unknown"
            self._cancel(work, "internal_error")

    def _finish_waiters(self, owner):
        for waiter in tuple(self.waiters):
            if waiter.request.operation == owner.request.operation:
                waiter.error, waiter.outcome, waiter.partial = owner.error, owner.outcome, owner.partial
                self._finish(waiter, owner.result)
                self.waiters.remove(waiter)

    def begin_shutdown(self, deadline, *, failure=False):
        """Cancel emission before recording, with a capped cleanup allowance."""
        self._guard()
        self.stopping = True
        works = [*self.queue, self.active, self.lifecycle, self.pending_stop, *self.waiters]
        seen = set()
        for work in works:
            if work is None or id(work) in seen or work.terminal:
                continue
            seen.add(id(work))
            failed = failure and work.request.operation not in CONTROL_OPERATIONS
            self._cancel(work, 'session_failed' if failed else 'cancelled',
                         ContractError('session_failed', 'Essential session health failed.') if failed else None)
            work.cleanup_deadline = min(work.cleanup_deadline, deadline)
        for work in self.finishing:
            work.settle_by = min(work.settle_by, deadline)

    def drain_shutdown(self, deadline):
        self._guard()
        for work in self.finishing:
            work.settle_by = min(work.settle_by, deadline)
        self.settle()
        works = [self.active, self.lifecycle, self.pending_stop]
        for work in works:
            if work is not None and not work.terminal:
                work.cleanup_deadline = min(work.cleanup_deadline, deadline)
                self._advance(work)
        # A finishing work's cleanup is complete; only its response waits for records.
        return all(work is None or work.terminal or work.finishing for work in works)

    def tick(self):
        self._guard()
        now = self.clock()
        if self.children is not None:
            self.children.poll(now)
        self.settle()
        for work in tuple(self.queue):
            if not work.terminal and self.clock() >= work.admission.deadline:
                self._cancel(work, "timeout")
        self.queue = deque(work for work in self.queue if not work.terminal and not work.finishing)
        for work in tuple(self.waiters):
            if self.clock() >= work.admission.deadline:
                work.error = ContractError("timeout", "Control waiter deadline expired.")
                self._finish(work)
                self.waiters.remove(work)
        # A pending stop owns its original deadline while superseded reset
        # cleanup still owns mutation. Expiry reports failure/escalates now; it
        # does not execute a second task or grant a renewed shutdown budget.
        if (self.pending_stop is not None and not self.pending_stop.terminal
                and self.clock() >= self.pending_stop.admission.deadline):
            self._cancel(self.pending_stop, "timeout")
            self._finish_waiters(self.pending_stop)
        if self.active is not None:
            self._advance(self.active)
        if self.lifecycle is not None:
            owner = self.lifecycle
            if self.clock() >= owner.admission.deadline and owner.error is None:
                self._cancel(owner, "timeout")
            if self.active is None:
                self._advance(owner)
            if owner.terminal or owner.finishing:
                self._finish_waiters(owner)
                self.lifecycle, self.pending_stop = self.pending_stop, None
            return
        if self.active is None and not self.stopping and not self.unavailable and self.queue:
            self.active = self.queue.popleft()
            self._advance(self.active)
        if self.unavailable:
            for work in tuple(self.queue):
                if not work.terminal:
                    work.error = ContractError("session_unavailable", "Cleanup could not be confirmed.")
                    self._finish(work)
            self.queue.clear()
