"""Python-only infrastructure fixture; no production desktop capability."""
import json
from pathlib import Path
import sys
import time
from agent_desktop.worker import run

MARKERS = Path(sys.argv[3])


def mark(event, request):
    with MARKERS.open('a') as stream:
        stream.write(json.dumps({'event': event, 'request_id': request.request_id, 'at': time.monotonic()}) + '\n')


class Task:
    cleanup_seconds = .1
    def __init__(self, request, context):
        self.request, self.context = request, context
        self.first = True
    def step(self, now):
        if self.first:
            self.first = False
            mark('effect', self.request)
            self.context.effects({'app': {'generation': self.request.expected_generation, 'application_id': 'fixture'}})
        if self.request.operation == 'type':
            return None
        return {'cwd': self.request.arguments.get('cwd'), 'argv': self.request.arguments.get('argv'),
                'output': self.request.arguments.get('output'), 'desktop_ready': False}
    def request_cancel(self, code):
        mark('release', self.request)
    def cleanup(self, now):
        mark('cleanup', self.request)
        return True


run('default', sys.argv[1], artifacts=sys.argv[2], factory=Task,
    capabilities={'session.stop'})
