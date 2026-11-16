import json,subprocess,sys
from pathlib import Path
class Dispatcher:
    """Consumes a complete external assignment; it does not perform placement."""
    def __init__(self,allocator,root):self.allocator=allocator;self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True);self.children={};self.log=[]
    def execute(self,ticket,members):
        stage=ticket['stage_id'];by_id={m['worker_id']:m for m in members};outputs={}
        for worker,node in ticket['assignment'].items():
            folder=self.root/node;folder.mkdir(exist_ok=True);out=folder/(stage+'-'+worker+'.json')
            request={'input':ticket['inputs'][worker]['path'],'output':str(out),'factor':by_id[worker]['factor']}
            request_path=folder/(stage+'-'+worker+'-request.json');request_path.write_text(json.dumps(request))
            process=subprocess.Popen([sys.executable,str(Path(__file__).parents[1]/'worker.py'),str(request_path)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            assert process.stdout.readline().strip()=='READY';self.children[worker]=process;outputs[worker]=str(out);self.log.append({'worker':worker,'node':node,'state':'READY','input':request['input']})
        for process in self.children.values():process.stdin.write('GO\n');process.stdin.flush()
        for worker,process in self.children.items():
            receipt=process.stdout.readline();process.wait();self.log.append({'worker':worker,'receipt':receipt.strip(),'exit':process.returncode})
        self.allocator.release_group(stage)
        return {'status':'complete','outputs':outputs,'log':self.log}
    def cleanup(self):
        for process in self.children.values():
            if process.poll() is None:process.terminate();process.wait()
