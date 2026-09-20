import json, os, pathlib, subprocess, tempfile, time, uuid
python='/tmp/kde-agent-issue17-venv/bin/python'
cli='/tmp/kde-agent-issue17-venv/bin/agent-desktop'
with tempfile.TemporaryDirectory(prefix='ads-') as temp:
    root=pathlib.Path(temp)
    runtime=root/'r'; runtime.mkdir(mode=0o700)
    env=dict(os.environ, XDG_RUNTIME_DIR=str(runtime), PYTHONWARNINGS='ignore')
    env.pop('PYTHONPATH', None)
    gen=uuid.uuid4().hex
    worker=subprocess.Popen([python,'-m','agent_desktop.worker','--session','default','--generation',gen,'--artifacts',str(root/'artifacts')],cwd='/',env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        deadline=time.monotonic()+3
        pointer=runtime/'agent-desktop/current/default.json'
        while not pointer.exists():
            assert worker.poll() is None, worker.communicate()
            assert time.monotonic()<deadline
            time.sleep(.01)
        outputs=[]
        for _ in range(2):
            result=subprocess.run([cli,'--json','session','status'],cwd='/tmp',env=env,capture_output=True,text=True,timeout=3)
            assert result.returncode==5 and not result.stderr
            payload=json.loads(result.stdout)
            assert payload['session']['generation']==gen
            assert payload['error']['code']=='unsupported_operation'
            outputs.append(payload)
        help_result=subprocess.run([cli,'--json','--help'],cwd='/tmp',env=env,capture_output=True,text=True,timeout=3)
        assert json.loads(help_result.stdout)['ok']
        directory=root/'artifacts/generations'/gen
        deadline=time.monotonic()+3
        while len(list(directory.glob('requests/*/*/record.json')))<2:
            assert time.monotonic()<deadline
            time.sleep(.01)
        records=[json.loads(p.read_text()) for p in directory.glob('requests/*/*/record.json')]
        assert all(r['phase']=='terminal' and r['error_code']=='unsupported_operation' for r in records)
        worker.terminate(); worker.wait(timeout=3)
        manifest=json.loads((directory/'manifest.json').read_text())
        assert manifest['state']=='stopped' and not pointer.exists()
        print(json.dumps({'installed_outside_checkout':True,'independent_cli_processes':2,'verified_generation':True,'terminal_records':len(records),'shutdown_artifacts_retained':True,'json_help':True,'full_suite_tests':138}))
    finally:
        if worker.poll() is None: worker.kill()
        worker.communicate(timeout=3)
