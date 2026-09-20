"""Internal foreground transport worker. No production desktop readiness yet."""
from __future__ import annotations

import argparse
import signal
import sys
from .contracts import ContractError, dispatch
from .runtime import Endpoint
from .transport import Server


def unsupported(request, admission):
    dispatch(request)


def run(name, generation, *, handler=unsupported):
    # Internal Python injection is for tests and future owners, never a CLI plugin.
    from gi.repository import GLib
    endpoint = Endpoint(name, generation)
    try:
        server = Server(endpoint, GLib, handler)
    except BaseException:
        endpoint.close()
        raise
    loop = GLib.MainLoop()
    sources = [GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, lambda: (loop.quit(), False)[1])
               for sig in (signal.SIGTERM, signal.SIGINT)]
    try:
        loop.run()
    finally:
        # Fired unix signal sources remove themselves. Find before removal.
        context = GLib.MainContext.default()
        for source in sources:
            if context.find_source_by_id(source) is not None:
                GLib.source_remove(source)
        server.close()


def main(argv=None):
    class Parser(argparse.ArgumentParser):
        def error(self, message):
            raise ContractError("invalid_arguments", "Invalid worker arguments.")
    parser = Parser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--session", required=True)
    parser.add_argument("--generation", required=True)
    try:
        args = parser.parse_args(argv)
        run(args.session, args.generation)
        return 0
    except ImportError:
        print("agent-desktop worker: prerequisite_missing: distribution PyGObject is required.", file=sys.stderr)
        return 3
    except ContractError as error:
        print(f"agent-desktop worker: {error.code}: {error.message}", file=sys.stderr)
        from .contracts import EXIT_CODES
        return EXIT_CODES[error.code]
    except Exception as error:
        print(f"agent-desktop worker: internal_error ({type(error).__name__}).", file=sys.stderr)
        return 70


if __name__ == "__main__":
    raise SystemExit(main())
