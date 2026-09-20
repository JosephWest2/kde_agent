"""Real service ownership fixture. Children have no desktop or control effects."""
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

name, generation, artifacts, source = sys.argv[1:5]
if source != '-':
    sys.path.insert(0, source)
from agent_desktop.worker import run

root = Path(artifacts) / 'generations' / generation
child_code = '''import json,os,pathlib,subprocess,sys,time
child = subprocess.Popen([sys.executable, '-I', '-c', 'import time; time.sleep(120)'])
def identity(pid):
    text=pathlib.Path('/proc',str(pid),'stat').read_text()
    return {'pid':pid,'start_ticks':text.rsplit(')',1)[1].split()[19]}
pathlib.Path(sys.argv[1]).write_text(json.dumps([identity(os.getpid()),identity(child.pid)]))
time.sleep(120)
'''
child = subprocess.Popen([sys.executable, '-I', '-c', child_code, str(root / 'fixture-children.json')])
def freeze_with_record_lock(signum, frame):
    with (root / 'record.lock').open('r') as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        (root / 'fixture-lock-held').write_text('held')
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        os.kill(os.getpid(), signal.SIGSTOP)
signal.signal(signal.SIGUSR1, freeze_with_record_lock)
run(name, generation, artifacts=artifacts, managed=True)
