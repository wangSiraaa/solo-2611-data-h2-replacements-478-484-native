import json,sys
from pathlib import Path
request=json.loads(Path(sys.argv[1]).read_text())
print('READY',flush=True)
if sys.stdin.readline().strip()!='GO':sys.exit(2)
rows=json.loads(Path(request['input']).read_text())
result=[{'row_id':row['row_id'],'value':row['value']*request['factor']} for row in rows]
Path(request['output']).write_text(json.dumps(result));print(json.dumps({'status':'complete','rows':len(result)}),flush=True)
