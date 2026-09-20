"""One-shot client and nonblocking GLib-owned transport; no desktop adapters."""
from __future__ import annotations

from dataclasses import replace
import select
import socket
import time
import uuid

from .contracts import ContractError, response
from .protocol import CancelRequest, Decoder, cancel_from_wire, encode, request_from_wire, validate_response
from .runtime import Runtime, peer_owner

CONNECT_SECONDS = 1.0
FRAME_SECONDS = 1.0
CLEANUP_RESERVE = 16.0
MAX_CONNECTIONS = 32


def exchange(request, *, deadline=None):
    """Pin once, connect once, send once. Never replay even a partial send."""
    overall_deadline = deadline
    def budget(limit):
        remaining = limit if overall_deadline is None else min(limit, overall_deadline - time.monotonic())
        if remaining <= 0:
            raise TimeoutError
        return remaining
    sent = False
    phase = "discovery"
    sock = None
    try:
        runtime = Runtime()
        if request.operation in {"input.reset", "session.stop"}:
            generation, path = runtime.discover(request.session, request.expected_generation, priority=True)
        else:
            generation, path = runtime.discover(request.session, request.expected_generation)
        wire_request = replace(request, expected_generation=generation)
        frame = encode(wire_request.payload())
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        phase = "connect"
        sock.settimeout(budget(CONNECT_SECONDS))
        sock.connect(str(path))
        peer_owner(sock)
        phase = "send"
        deadline = time.monotonic() + budget(FRAME_SECONDS)
        offset = 0
        while offset < len(frame):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            sock.settimeout(remaining)
            sent = True  # A failing send can still have transmitted bytes.
            count = sock.send(frame[offset:])
            if count == 0:
                raise ConnectionError
            offset += count
        phase = "response"
        deadline = time.monotonic() + budget(request.timeout_seconds + CLEANUP_RESERVE + FRAME_SECONDS)
        decoder = Decoder()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            sock.settimeout(remaining)
            data = sock.recv(65536)
            if not data:
                raise ConnectionError
            value = decoder.feed(data)
            if value is not None:
                return validate_response(value, request, generation)
    except KeyboardInterrupt:
        if sent:
            try:
                # Never resolve again. The original EOF remains a fallback if
                # priority connect/send fails, with no acknowledgement wait.
                cancel = CancelRequest(uuid.uuid4().hex, request.session, generation, request.request_id)
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as control:
                    limit = time.monotonic() + budget(.1)
                    control.settimeout(budget(.1))
                    control.connect(str(path.with_name("priority.sock")))
                    peer_owner(control)
                    control.settimeout(budget(limit - time.monotonic()))
                    control.sendall(encode(cancel.payload()))
            except (OSError, ContractError, KeyboardInterrupt):
                pass
        raise ContractError("cancelled", "Request interrupted.", outcome="unknown" if sent else "not_started") from None
    except ContractError as error:
        if not sent:
            error.context.setdefault("expected_generation", request.expected_generation)
            raise
        raise ContractError("completion_unknown", "A valid completion response was not received; do not replay.",
                            outcome="unknown", context={"phase": phase}) from None
    except OSError:
        code = "completion_unknown" if sent else "session_unavailable" if phase == "connect" else "transport_error"
        raise ContractError(code, "Session transport failed; request was not retried.",
                            outcome="unknown" if sent else "not_started", context={"phase": phase}) from None
    finally:
        if sock is not None:
            sock.close()


class Admission:
    """One event-loop owner's asynchronous completion/disconnect boundary.

    Handlers must return promptly; #16 supplies cancellable task scheduling.
    A disconnected client does not complete or undo its admitted request.
    """
    def __init__(self, connection, request):
        self.connection = connection
        self.request = request
        self.admitted_at = time.monotonic()
        self.deadline = self.admitted_at + request.timeout_seconds
        self.disconnected = False
        self.on_disconnect = None
        self.terminal = False
        self.on_terminal = None
        self.final_payload = None

    def complete(self, *, result=None, error=None):
        if self.terminal:
            return
        self.terminal = True
        server = self.connection.server
        if server.active.get(self.request.request_id) is self:
            del server.active[self.request.request_id]
        if error is None and time.monotonic() >= self.deadline:
            error = ContractError("timeout", "Request deadline expired.", outcome="unknown",
                                  partial_result=result if isinstance(result, dict) else None)
        payload = response(self.request.request_id, self.request.operation, session=server.name,
                           generation=server.generation, result=result, error=error)
        self.connection.reply(payload, dispatched=True)
        # Transport freezes the final deadline/serialization outcome first.
        # This notification cannot veto or change that accepted result. Durable
        # consumers record finalizing before acceptance and then this exact value.
        self.final_payload = self.connection.final_payload or payload
        if self.on_terminal is not None:
            try:
                self.on_terminal(self.final_payload)
            except Exception:
                pass

    def disconnect(self):
        if self.disconnected:
            return
        self.disconnected = True
        if not self.terminal and self.on_disconnect is not None:
            self.on_disconnect()


class Connection:
    def __init__(self, server, sock, *, priority=False):
        self.server, self.sock = server, sock
        self.priority = priority
        self.decoder = Decoder()
        self.watch = 0
        self.deadline = time.monotonic() + FRAME_SECONDS
        self.admission = None
        self.output = None
        self.final_payload = None
        self.offset = 0
        self.closed = False
        self.arm()

    def arm(self):
        # The server's bounded shared service turn polls readiness. Independent
        # permanently-ready sources cannot starve deadlines or action steps.
        pass

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.watch:
            self.server.glib.source_remove(self.watch)
            self.watch = 0
        self.sock.close()
        self.server.connections.discard(self)
        if self.admission is not None:
            self.admission.disconnect()

    def reply(self, payload, *, dispatched=False):
        try:
            # Validate before encoding and before any header leaves this process.
            if self.admission is not None:
                validate_response(payload, self.admission.request, self.server.generation)
            self.output = encode(payload)
            if (self.admission is not None and payload["ok"]
                    and time.monotonic() >= self.admission.deadline):
                payload = response(self.admission.request.request_id,
                                   self.admission.request.operation,
                                   session=self.server.name, generation=self.server.generation,
                                   error=ContractError("timeout", "Request deadline expired.",
                                                       outcome="unknown", partial_result=payload["result"]))
                self.output = encode(payload)
            self.final_payload = payload
        except (ContractError, TypeError, ValueError, RecursionError):
            retained = None
            candidate = payload.get("error") if isinstance(payload, dict) else None
            partial = candidate.get("partial_result") if isinstance(candidate, dict) else payload.get("result")
            if isinstance(partial, dict):
                # Keep only individually safe generation-qualified handles, not a huge result.
                from .contracts import handle
                retained = {}
                for field, kind in (("app", "app"), ("application", "app"), ("window", "window")):
                    try:
                        if field in partial:
                            value = handle(partial[field], kind)
                            if value["generation"] == self.server.generation and len(encode(value)) < 4096:
                                retained[field] = value
                    except ContractError:
                        pass
                retained = retained or None
            error = ContractError("completion_unknown" if dispatched else "protocol_error",
                                  "Response could not be represented safely.",
                                  outcome="unknown" if dispatched else "not_started", partial_result=retained)
            request = self.admission.request if self.admission else None
            self.final_payload = response(request.request_id if request else uuid.uuid4().hex,
                                          request.operation if request else None,
                                          session=self.server.name if request else None,
                                          generation=self.server.generation if request else None, error=error)
            self.output = encode(self.final_payload)
        self.offset = 0
        self.deadline = time.monotonic() + FRAME_SECONDS
        if self.watch:
            self.server.glib.source_remove(self.watch)
            self.watch = 0
        self.arm()

    def ready(self, _fd, condition):
        # A callback owns one recv/send, so a busy client cannot drain the loop.
        self.watch = 0
        glib = self.server.glib
        try:
            if condition & (glib.IO_IN | glib.IO_HUP | glib.IO_ERR):
                data = self.sock.recv(65536)
                if not data:
                    self.close()
                    return False
                if self.admission is not None or self.output is not None:
                    # Additional bytes never dispatch another operation.
                    self.close()
                    return False
                value = self.decoder.feed(data)
                if value is not None:
                    # A frame may end exactly at the recv chunk boundary. Check
                    # already queued bytes once; do not wait for future bytes.
                    try:
                        trailing = self.sock.recv(1, socket.MSG_PEEK)
                    except BlockingIOError:
                        trailing = None
                    if trailing == b"" and not self.priority:
                        self.close()
                        return False
                    if trailing:
                        raise ContractError("protocol_error", "Trailing request bytes are not allowed.")
                    self.dispatch(value)
            if not self.closed and condition & glib.IO_OUT and self.output is not None:
                if time.monotonic() >= self.deadline:
                    self.close()
                else:
                    self.offset += self.sock.send(self.output[self.offset:self.offset + 65536])
                    if self.offset == len(self.output):
                        self.close()
        except BlockingIOError:
            pass
        except ContractError as error:
            self.reply(response(uuid.uuid4().hex, None, error=error))
        except OSError:
            self.close()
        self.arm()
        return False

    def dispatch(self, value):
        request = None
        try:
            if isinstance(value, dict) and value.get("operation") == "request.cancel":
                request = cancel_from_wire(value)
            else:
                request = request_from_wire(value)
            control = request.operation in {"request.cancel", "input.reset", "session.stop"}
            if control != self.priority:
                raise ContractError("protocol_error", "Request used the wrong endpoint.")
            if request.session != self.server.name or request.expected_generation != self.server.generation:
                raise ContractError("generation_mismatch", "Request identity differs from this worker.")
            if time.monotonic() >= self.deadline:
                raise ContractError("timeout", "Request frame deadline expired.")
            if request.request_id in self.server.active:
                raise ContractError("protocol_error", "Request ID is already active.")
            admission = Admission(self, request)
            self.admission = admission
            self.server.active[request.request_id] = admission
            self.deadline = admission.deadline + CLEANUP_RESERVE
            try:
                if time.monotonic() >= admission.deadline:
                    raise ContractError("timeout", "Request deadline expired before dispatch.")
                if request.operation == "request.cancel":
                    accepted = self.server.cancel(request.target_request_id)
                    admission.complete(result={"cancel_requested": accepted})
                else:
                    self.server.handler(request, admission)
            except ContractError as error:
                admission.complete(error=error)
            except Exception:
                admission.complete(error=ContractError("internal_error", "Worker handler failed.", outcome="unknown"))
        except ContractError as error:
            self.reply(response(request.request_id if request else uuid.uuid4().hex,
                                request.operation if request else None,
                                session=self.server.name if request else None,
                                generation=self.server.generation if request else None, error=error))


class Server:
    """One bounded service turn: priority, ordinary, deadlines, scheduler.

    Both socket sets are capped independently. Ready connections rotate after
    each turn and only eight per class run, so control floods cannot starve work.
    All callbacks share a single GLib source, not competing source priorities.
    """
    def __init__(self, endpoint, glib, handler, *, cancel=None, after_io=None):
        self.endpoint, self.glib, self.handler = endpoint, glib, handler
        self.name, self.generation = endpoint.name, endpoint.generation
        self.connections = set()
        self.active = {}
        self.cancel = cancel or (lambda request_id: False)
        self.after_io = after_io or (lambda: None)
        self.order = {True: [], False: []}
        self.timer = glib.timeout_add(5, self.service)

    def accept(self, priority):
        listener = self.endpoint.priority_listener if priority else self.endpoint.listener
        cap = 8 if priority else MAX_CONNECTIONS
        count = sum(c.priority == priority for c in self.connections)
        for _ in range(8):
            try:
                sock, _ = listener.accept()
            except BlockingIOError:
                break
            try:
                sock.setblocking(False)
                peer_owner(sock)
                if count >= cap:
                    sock.close()
                    continue
                connection = Connection(self, sock, priority=priority)
                self.connections.add(connection)
                self.order[priority].append(connection)
                count += 1
            except (OSError, ContractError):
                sock.close()

    def service(self):
        for priority in (True, False):
            self.accept(priority)
            pending = [c for c in self.order[priority] if not c.closed]
            self.order[priority] = pending
            if not pending:
                continue
            readers, writers, _ = select.select(
                [c.sock for c in pending if c.output is None],
                [c.sock for c in pending if c.output is not None], [], 0)
            ready_r, ready_w = set(readers), set(writers)
            served = []
            for connection in pending:
                events = (self.glib.IO_IN if connection.sock in ready_r else 0)
                events |= (self.glib.IO_OUT if connection.sock in ready_w else 0)
                if events:
                    connection.ready(connection.sock.fileno(), events)
                    served.append(connection)
                    if len(served) >= 8:
                        break
            self.order[priority] = [c for c in pending if c not in served] + served
        self.expire()
        self.after_io()
        return True

    def expire(self):
        now = time.monotonic()
        for connection in tuple(self.connections):
            if now >= connection.deadline:
                connection.close()
        return True

    def close(self):
        self.glib.source_remove(self.timer)
        for connection in tuple(self.connections):
            connection.close()
        self.endpoint.close()
