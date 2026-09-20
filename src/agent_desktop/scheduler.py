"""Single-owner, bounded-step scheduling. No desktop bindings or native imports.

Task factory(request, context) returns an object with step(now) -> result|None,
request_cancel(reason), cleanup(now) -> bool and cleanup_seconds (<=16). Calls
must be bounded/nonblocking. Only this owner may call task methods. Context
records effects before cancellation can run; observers never receive argv/env.
"""
from __future__ import annotations

from collections import deque
from copy import deepcopy
from dataclasses import dataclass
import time

from .contracts import ContractError, dispatch

CONTROL_OPERATIONS = frozenset({"input.reset", "session.stop"})
MAX_ORDINARY = 32


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
        if self.work.observing_failed and self.work.request.operation not in CONTROL_OPERATIONS:
            raise ContractError("artifact_failed", "Request record could not be preserved.")


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
        self.input_available = True
        self._observing = False

    def _guard(self):
        if self._observing:
            raise RuntimeError("Observers cannot re-enter scheduler mutation")

    def _observe(self, work, event):
        if self.observer is None:
            return
        self._observing = True
        try:
            self.observer(deepcopy({"request_id": work.request.request_id,
                           "operation": work.request.operation,
                           "session": work.request.session,
                           "generation": work.request.expected_generation,
                           "event": event, "outcome": work.outcome,
                           "error_code": work.error.code if work.error else None,
                           "partial_result": work.partial}))
        except Exception:
            work.observing_failed = True
        finally:
            self._observing = False

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
        if request.operation in {"key", "type", "click"} and not self.input_available:
            admission.complete(error=ContractError("input_unavailable", "Input requires successful reset."))
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
        if work is None or work.terminal:
            return False
        self._cancel(work, code)
        return True

    def _cancel(self, work, code, error=None):
        if work.error is not None:
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
        self.input_available = False
        work.outcome = "unknown"
        try:
            self.escalate("cleanup_unconfirmed")
        except Exception:
            pass

    def _finish(self, work, result=None):
        if work.terminal:
            return
        if work.error is None and self.clock() >= work.admission.deadline:
            work.error = ContractError("timeout", "Request deadline expired.")
        self._observe(work, "finalizing")
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

    def _control(self, work):
        op = work.request.operation
        if op not in self.capabilities:
            work.admission.complete(error=ContractError("unsupported_operation", "Operation is not implemented yet."))
            return
        if self.clock() >= work.admission.deadline:
            work.admission.complete(error=ContractError("timeout", "Control deadline expired."))
            return
        if op == "input.reset" and (self.stopping or self.unavailable):
            work.admission.complete(error=ContractError("session_unavailable", "Session is unavailable."))
            return
        existing = self.pending_stop if op == "session.stop" and self.pending_stop is not None else self.lifecycle
        joining = existing is not None and existing.request.operation == op
        if joining and len(self.waiters) >= 32:
            work.admission.complete(error=ContractError("session_unavailable", "Control waiters are full."))
            return
        owner_deadline = existing.admission.deadline if joining else work.admission.deadline
        self.input_available = False
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
            else:
                self.waiters.append(work)
        else:
            work.admission.complete(error=ContractError("session_unavailable", "Shutdown is in progress."))
            return
        self._observe(work, "control_admitted")
        # No disconnect hook: required lifecycle cleanup belongs to the worker.

    def _advance(self, work, now):
        if work.terminal:
            return
        if work.error is None and now >= work.admission.deadline:
            self._cancel(work, "timeout")
        if work.terminal:
            return
        if work.observing_failed and work.error is None and work.request.operation not in CONTROL_OPERATIONS:
            self._cancel(work, "artifact_failed")
        if work.error is not None:
            try:
                clean = work.task is None or work.task.cleanup(now)
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

    def tick(self):
        self._guard()
        now = self.clock()
        if self.children is not None:
            self.children.poll(now)
        for work in tuple(self.queue):
            if not work.terminal and now >= work.admission.deadline:
                self._cancel(work, "timeout")
        self.queue = deque(work for work in self.queue if not work.terminal)
        for work in tuple(self.waiters):
            if now >= work.admission.deadline:
                work.error = ContractError("timeout", "Control waiter deadline expired.")
                self._finish(work)
                self.waiters.remove(work)
        if self.active is not None:
            self._advance(self.active, now)
        if self.lifecycle is not None:
            owner = self.lifecycle
            if now >= owner.admission.deadline and owner.error is None:
                self._cancel(owner, "timeout")
            if self.active is None:
                self._advance(owner, now)
            if owner.terminal:
                for waiter in tuple(self.waiters):
                    if waiter.request.operation == owner.request.operation:
                        waiter.error, waiter.outcome, waiter.partial = owner.error, owner.outcome, owner.partial
                        self._finish(waiter, owner.result)
                        self.waiters.remove(waiter)
                if owner.request.operation == "input.reset":
                    self.input_available = owner.error is None and not owner.observing_failed and not self.stopping
                self.lifecycle, self.pending_stop = self.pending_stop, None
            return
        if self.active is None and not self.stopping and not self.unavailable and self.queue:
            self.active = self.queue.popleft()
            if self.active.request.operation in {"key", "type", "click"} and not self.input_available:
                self.active.error = ContractError("input_unavailable", "Input requires successful reset.")
                self._finish(self.active)
            else:
                self._advance(self.active, self.clock())
        if self.unavailable:
            for work in tuple(self.queue):
                if not work.terminal:
                    work.error = ContractError("session_unavailable", "Cleanup could not be confirmed.")
                    self._finish(work)
            self.queue.clear()
