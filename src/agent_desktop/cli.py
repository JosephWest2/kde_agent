"""Local CLI parser. Diagnostics never repeat untrusted parser/exception text."""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid

from . import __version__
from .contracts import ARGUMENTS, EXIT_CODES, OPERATIONS, SUPPORTED_OPERATIONS, ContractError, dispatch, make_request, response, with_refs


class HelpRequested(Exception):
    def __init__(self, help_text):
        self.help_text = help_text


class Parser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, allow_abbrev=False, **kwargs)

    def error(self, message):
        # argparse messages can contain arbitrary argv values, including secrets.
        raise ContractError("invalid_arguments", "Invalid or missing argument; use --help.")

    def print_help(self, file=None):
        raise HelpRequested(self.format_help())


def parser():
    root = Parser(prog="agent-desktop", description="Private headless KWin desktop for testing GUI applications. "
                  "Commands marked (not yet implemented) return unsupported_operation.")
    root.add_argument("--json", action="store_true", help="emit one JSON result, including help and errors")
    root.add_argument("--version", action="store_true", help="show package version")
    commands = root.add_subparsers(dest="command", required=True)
    leaves = {}
    def note(operation):
        return None if operation in SUPPORTED_OPERATIONS else "(not yet implemented)"
    for operation in OPERATIONS:
        if "." not in operation:
            leaves[operation] = commands.add_parser(operation, help=note(operation))
    for family, actions in (("session", ("start", "status", "stop")),):
        implemented = any(f"{family}.{action}" in SUPPORTED_OPERATIONS for action in actions)
        group = commands.add_parser(family, help=None if implemented else "(not yet implemented)")
        group.add_argument("--json", action="store_true", help="emit one JSON result")
        sub = group.add_subparsers(dest="action", required=True)
        for action in actions:
            leaves[f"{family}.{action}"] = sub.add_parser(action, help=note(f"{family}.{action}"))
    for operation, leaf in leaves.items():
        default, maximum, _ = OPERATIONS[operation]
        leaf.set_defaults(operation=operation)
        leaf.add_argument("--json", action="store_true", help="emit one JSON result")
        leaf.add_argument("--timeout", default=None, metavar="SECONDS", help=f"work budget (default {default:g}s, maximum {maximum:g}s)")
        if operation != "doctor":
            leaf.add_argument("--session", default="default", metavar="NAME")
            leaf.add_argument("--generation", metavar="TOKEN", help="expected session generation; omitted resolves current generation")
        for target in ("app", "window"):
            if target in ARGUMENTS[operation]:
                leaf.add_argument(f"--{target}", metavar="GENERATION:ID")
    leaves["doctor"].add_argument("--dependency-root", default=".local/dependencies")
    leaves["session.start"].add_argument("--dependency-root", default=".local/dependencies")
    leaves["session.start"].add_argument("--mode", default="headless")
    leaves["session.start"].add_argument("--artifacts", default=".agent-desktop/artifacts")
    launch = leaves["launch"]
    launch.epilog = "Required: launch [options] -- PROGRAM [ARG ...]. No implicit shell."
    launch.add_argument("--cwd")
    launch.add_argument("--env", action="append", default=[], metavar="KEY=VALUE")
    launch.add_argument("--wait-window", action="store_true")
    leaves["wait"].add_argument("--for", dest="condition", required=True, metavar="window|focus|exit|title|gone",
                                help="window/exit take --app; focus/title/gone take --window")
    leaves["wait"].add_argument("--match", metavar="TEXT", help="title wait: case-sensitive substring (at most 256 characters)")
    leaves["wait"].add_argument("--regex", action="store_true", help="title wait: --match is a Python re pattern (compiled and searched in a helper, at most 100ms CPU)")
    leaves["key"].add_argument("chord", metavar="CHORD")
    leaves["key"].add_argument("--hold", default=.05, metavar="SECONDS")
    leaves["type"].add_argument("text", metavar="TEXT")
    for operation in ("click", "move", "scroll"):
        for axis in ("x", "y"):
            leaves[operation].add_argument(f"--{axis}", required=True, metavar="INT",
                                           help="client-area pixels with --window, else screen pixels")
    leaves["click"].add_argument("--button", default="left", metavar="left|right|middle")
    leaves["click"].add_argument("--count", default="1", metavar="1-3", help="2 = double click, 3 = triple click")
    for operation in ("click", "scroll", "drag"):
        leaves[operation].add_argument("--modifiers", metavar="ctrl,shift,alt",
                                       help="keys held around the pointer input (needs --window)")
    leaves["drag"].add_argument("--from", dest="from", required=True, metavar="X,Y",
                                help="client-area pixels where the button goes down")
    leaves["drag"].add_argument("--to", required=True, metavar="X,Y", help="client-area pixels where it comes up")
    leaves["drag"].add_argument("--button", default="left", metavar="left|right|middle")
    leaves["drag"].add_argument("--duration", default=None, metavar="0-2000",
                                help="milliseconds from press to the last motion (default 300)")
    leaves["scroll"].add_argument("--dy", default=None, metavar="-50..50",
                                  help="vertical wheel steps: positive scrolls down, negative up (default 0)")
    leaves["scroll"].add_argument("--dx", default=None, metavar="-50..50",
                                  help="horizontal wheel steps: positive scrolls right, negative left (default 0)")
    leaves["screenshot"].add_argument("--output")
    leaves["logs"].add_argument("--source", default="all", metavar="all|application|worker|compositor|bus")
    leaves["logs"].add_argument("--tail", default="20", metavar="0-200", help="last lines of each log to include")
    return root


def split_cli(argv):
    """Only toolkit tokens before the first literal delimiter select JSON mode."""
    boundary = argv.index("--") if "--" in argv else len(argv)
    prefix, tail = argv[:boundary], argv[boundary:]
    json_mode = "--json" in prefix
    # Detect rendering mode without rewriting option/value adjacency.
    return prefix, tail, json_mode


def parse_request(argv, request_id, caller_cwd):
    prefix, tail, json_mode = split_cli(argv)
    root = parser()
    if [token for token in prefix if token != "--json"] == ["--version"] and not tail:
        return None, dict(version=__version__), "version", json_mode
    if "--version" in prefix:
        raise ContractError("invalid_arguments", "Use --version without a command.")
    # Root accepts only valueless --json before a command. Keep every token
    # in its original position when argparse validates options and their values.
    launch = next((token for token in prefix if token != "--json"), None) == "launch"
    try:
        namespace = root.parse_args(prefix if launch else prefix + tail)
    except HelpRequested as help_result:
        return None, dict(help=help_result.help_text), "help", json_mode
    values = vars(namespace)
    operation = values["operation"]
    args = {key: value for key, value in values.items() if key in ARGUMENTS[operation]}
    if launch:
        if not tail or len(tail) == 1:
            raise ContractError("invalid_arguments", "Launch requires -- followed by a program.")
        args["argv"] = tail[1:]
    request = make_request(operation, arguments=args, caller_cwd=caller_cwd,
                           session=values.get("session"), expected_generation=values.get("generation"),
                           timeout_seconds=values.get("timeout"), request_id=request_id)
    if operation == "wait" and request.arguments.get("regex"):
        # Early, friendly compile in the CLI process; the worker never compiles.
        from .title_regex import compile_check
        compile_check(request.arguments["match"])
    from .paths import normalize
    return normalize(request), None, operation, json_mode


def copy_output(payload, output):
    """Copy the worker's artifact PNG to --output (a file path, or an existing directory)."""
    result = payload["result"]
    target = output
    if os.path.isdir(target):
        target = os.path.join(target, result["capture_id"] + ".png")
    temporary = f"{target}.{uuid.uuid4().hex}.partial"
    error = None
    try:
        with open(result["path"], "rb") as source, open(temporary, "xb") as destination:
            destination.write(source.read())
        os.replace(temporary, target)
    except OSError:
        error = ContractError("artifact_failed", "Screenshot was captured but could not be copied to --output.",
                              context={"output": output}, outcome="partial", partial_result=result)
    except KeyboardInterrupt:
        error = ContractError("cancelled", "Screenshot was captured; copying to --output was interrupted.",
                              context={"output": output}, outcome="partial", partial_result=result)
    finally:
        if error is not None or not os.path.exists(target):
            try:
                os.unlink(temporary)
            except OSError:
                pass
    if error is not None:
        return dict(payload, ok=False, result=None, error=error.payload())
    payload = dict(payload)
    payload["result"] = result | {"output": target}
    return payload


def render(payload, json_mode):
    if json_mode:
        print(json.dumps(payload, ensure_ascii=True, allow_nan=False, separators=(",", ":")))
    elif not payload["ok"]:
        error = payload["error"]
        print(f"agent-desktop: {error['code']}: {error['message']}", file=sys.stderr)
    else:
        result = payload["result"]
        if "help" in result:
            print(result["help"], end="")
        elif "version" in result:
            print(f"agent-desktop {result['version']}")
        else:
            # Future commands supply structured results; this is deliberately simple.
            print(json.dumps(result, indent=2, ensure_ascii=True, allow_nan=False))


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    request_id = uuid.uuid4().hex
    json_mode = split_cli(argv)[2]
    request = None
    operation = None
    try:
        request, local_result, operation, json_mode = parse_request(argv, request_id, os.getcwd())
        if request is not None:
            if operation == "doctor":
                from .prerequisites import doctor
                payload = response(request_id, operation, result=doctor(request))
            elif operation == "session.start":
                from .lifecycle import Manager
                payload = Manager().start(request)
            elif operation in {"session.status", "session.stop"}:
                from .lifecycle import Manager
                payload = Manager().handle(request)
            else:
                from .transport import exchange
                payload = exchange(request)
                if (operation == "screenshot" and payload["ok"] and request.arguments.get("output")
                        and "capture_id" in payload["result"]):
                    payload = copy_output(payload, request.arguments["output"])
            status = 0 if payload["ok"] else EXIT_CODES[payload["error"]["code"]]
        else:
            result = dispatch(request) if request is not None else local_result
            payload = response(request_id, operation, session=request.session if request else None, result=result)
            status = 0
    except KeyboardInterrupt:
        error = ContractError("cancelled", "Request interrupted.", outcome="unknown" if request else "not_started")
        payload = response(request_id, operation, session=request.session if request else None, error=error)
        status = EXIT_CODES[error.code]
    except ContractError as error:
        payload = response(request_id, operation, session=request.session if request else None, error=error)
        status = EXIT_CODES[error.code]
    except Exception as error:
        # Do not emit exception messages, traceback source lines, argv or locals.
        print(f"agent-desktop internal diagnostic: {type(error).__name__}", file=sys.stderr)
        failure = ContractError("internal_error", "Internal command failure.", outcome="unknown" if request else "not_started")
        payload = response(request_id, operation, session=request.session if request else None, error=failure)
        status = EXIT_CODES[failure.code]
    render(with_refs(payload), json_mode)
    return status
