"""Install an immutable repository revision without stale build-directory files."""
from pathlib import Path
import hashlib
import json
import re
import subprocess
import sys
import tarfile
import tempfile

revision, environment, log = sys.argv[1:]
repository = Path.cwd()
commit = subprocess.check_output(['git', 'rev-parse', revision], text=True).strip()
root = Path(tempfile.mkdtemp(prefix='kde-clean-source-'))
archive = root / 'source.tar'
with archive.open('wb') as stream:
    subprocess.run(['git', 'archive', commit], stdout=stream, check=True)
source = root / 'source'
source.mkdir()
with tarfile.open(archive) as bundle:
    bundle.extractall(source, filter='data')
interpreter = str((repository / environment / 'bin/python').absolute())
with Path(log).open('w') as stream:
    subprocess.run([interpreter, '-m', 'pip', 'install', '--force-reinstall',
                    '--no-build-isolation', '--no-deps', str(source)],
                   stdout=stream, stderr=subprocess.STDOUT, check=True)
probe = '''import agent_desktop, hashlib, json
from pathlib import Path
root=Path(agent_desktop.__file__).parent
print(json.dumps({p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in root.iterdir() if p.suffix in ('.py','.js')}))'''
installed = json.loads(subprocess.check_output([interpreter, '-I', '-c', probe], text=True))
expected = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (source / 'src/agent_desktop').iterdir() if p.suffix in ('.py', '.js')}
assert installed == expected, 'Installed package differs from committed source archive'
wheel = re.search(r'filename=(\S+) size=(\d+) sha256=([0-9a-f]{64})', Path(log).read_text())
assert wheel is not None, 'Build log must identify the wheel installed by pip'
receipt = {'commit': commit, 'archive_source': str(source), 'archive': str(archive),
           'archive_sha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
           'interpreter': interpreter, 'verified_files': len(expected),
           'source_hashes': expected, 'installed_hashes': installed,
           'wheel': {'filename': wheel[1], 'size': int(wheel[2]), 'sha256': wheel[3],
                     'provenance': 'pip build/install log; ephemeral wheel cache is not retained'},
           'build_log': str(Path(log).resolve()),
           'build_log_sha256': hashlib.sha256(Path(log).read_bytes()).hexdigest(),
           'installer_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
Path(log + '.json').write_text(json.dumps(receipt, indent=2) + '\n')
print(json.dumps(receipt))
