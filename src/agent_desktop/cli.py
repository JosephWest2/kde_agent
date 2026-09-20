"""Local CLI parser. Diagnostics never repeat untrusted parser/exception text."""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid

from . import __version__
from .contracts import ARGUMENTS, EXIT_CODES, OPERATIONS, ContractError, dispatch, make_request, response


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
    root = Parser(prog="agent-desktop", description="Private headless KWin command scaffold. Desktop operations are not wired yet.")
    root.add_argument("--json", action="store_true", help="emit one JSON result, including help and errors")
    root.add_argument("--version", action="store_true", help="show package version")
    commands = root.add_subparsers(dest="command", required=True)
    leaves = {}
    for operation in OPERATIONS:
        if "." not in operation:
            leaves[operation] = commands.add_parser(operation)
    for family, actions in (("session", ("start", "status", "stop")), ("input", ("reset",))):
        group = commands.add_parser(family)
        group.add_argument("--json", action="store_true", help="emit one JSON result")
        sub = group.add_subparsers(dest="action", required=True)
        for action in actions:
            leaves[f"{family}.{action}"] = sub.add_parser(action)
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
    leaves["wait"].add_argument("--for", dest="condition", required=True, metavar="window|focus|exit")
    leaves["key"].add_argument("chord", metavar="CHORD")
    leaves["key"].add_argument("--hold", default=.05, metavar="SECONDS")
    leaves["type"].add_argument("text", metavar="TEXT")
    for axis in ("x", "y"):
        leaves["click"].add_argument(f"--{axis}", required=True)
    leaves["click"].add_argument("--button", default="left")
    leaves["screenshot"].add_argument("--output")
    leaves["logs"].add_argument("--source", default="all")
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
    from .paths import normalize
    return normalize(request), None, operation, json_mode


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
    render(payload, json_mode)
    return status
