"""Installed decoder/encoder timing at the documented raw/row limits."""
import json
import statistics
import sys
import time
import uuid
from pathlib import Path
from agent_desktop.window_types import Decoder
from agent_desktop.protocol import encode


def stats(values):
    values = sorted(values)
    return {'p50':statistics.median(values), 'p95':values[int(.95*(len(values)-1))], 'max':max(values)}


def main(output):
    base = {'schema_version':1, 'request_id':'query', 'active_uuid':None,
            'outputs':[{'name':'Virtual-0','width':1280,'height':720,'scale':None}], 'windows':[]}
    for index in range(256):
        base['windows'].append({'uuid':str(uuid.UUID(int=index+1)), 'pid':None, 'title':'x'*440,
                               'class':'y'*440, 'client':None, 'frame':None, 'active':False})
    raw=json.dumps(base,separators=(',',':')).encode()
    assert len(raw)<=262144
    remaining=262144-len(raw)
    for row in base['windows']:
        added = min(remaining,4096-440)
        row['title'] += 'x'*added
        remaining -= added
        if not remaining:
            break
    raw=json.dumps(base,separators=(',',':')).encode()
    result={'raw_bytes':len(raw),'rows':256,'iterations':100,'source_module':__import__('agent_desktop.window_types',fromlist=['x']).__file__}
    timings={'json_decode':[],'row_validation_total':[],'max_row_turn':[],'public_encode':[]}
    for _ in range(100):
        start=time.monotonic(); decoder=Decoder(raw,'query'); timings['json_decode'].append(time.monotonic()-start)
        turns=[]; done=False
        while not done:
            start=time.monotonic(); done=decoder.step(start+.002,time.monotonic); turns.append(time.monotonic()-start)
        timings['row_validation_total'].append(sum(turns));timings['max_row_turn'].append(max(turns))
        value={'windows':[w.wire('a'*32)|{'app':None,'association':{'reason':'missing_pid','process':None,'verified_at':None}} for w in decoder.windows]}
        start=time.monotonic(); encoded=encode(value);timings['public_encode'].append(time.monotonic()-start)
    result['encoded_bytes']=len(encoded);result['seconds']={k:stats(v) for k,v in timings.items()}
    Path(output).write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result))


if __name__=='__main__':
    main(sys.argv[1])
