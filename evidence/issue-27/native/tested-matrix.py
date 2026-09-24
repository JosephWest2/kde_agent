import json, pathlib, subprocess, sys
root=pathlib.Path.cwd()
prepared=root/'.local/issue27-release-source'
source=prepared/'source'
output=root/'.local/issue27-native-release'
output.mkdir()
binary='/home/josephwest/.t3/worktrees/kde-agent/t3code-c74ae35a/.local/issue9-clean/bin/kdotool'
plugin='/home/josephwest/.t3/worktrees/kde-agent/t3code-c74ae35a/.local/issue12-eis-fault/build/issue12_eis_fault.so'
subprocess.run([str(prepared/'venv/bin/python'),'-I',str(source/'evidence/issue-27/audit.py'),str(output/'audit')],check=True)
records=[]
for scenario in ('input','cancel','pause','removal','disconnect','reset-failure','focus-loss','slow-query','slow-external'):
    argv=['/usr/bin/python','-I',str(source/'tools/private_harness.py'),'run','--artifacts',str(output/'runs')]
    if scenario in ('pause','slow-external'): argv+=['--eis-fault-plugin',plugin]
    argv+=['--',str(prepared/'venv/bin/python'),'-I',str(source/'evidence/issue-27/installed_input.py'),binary,scenario]
    result=subprocess.run(argv,capture_output=True,text=True,timeout=120)
    (output/(scenario+'.stdout')).write_text(result.stdout)
    (output/(scenario+'.stderr')).write_text(result.stderr)
    record=dict(scenario=scenario,argv=argv,returncode=result.returncode,result=json.loads(result.stdout))
    records.append(record)
    (output/'selection.json').write_text(json.dumps(records,indent=2)+'\n')
    print(scenario,result.returncode,flush=True)
    if result.returncode: sys.exit(result.returncode)
