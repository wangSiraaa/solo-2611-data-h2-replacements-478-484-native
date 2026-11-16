import hashlib,itertools,json,tempfile
from pathlib import Path
from placement.allocator import Allocator
from execution.dispatcher import Dispatcher
def spec(path):return {'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
def main():
    with tempfile.TemporaryDirectory() as d:
        root=Path(d);nodes={'scarce':{'cpu':1,'io':1},'other':{'cpu':1,'io':1}};copies={}
        for node in nodes:
            p=root/node/'input.json';p.parent.mkdir();p.write_text(json.dumps([{'row_id':'r1','value':7}]));copies[node]=spec(p)
        normal=[{'worker_id':'only','allowed_nodes':['other'],'resources':{'cpu':1,'io':1},'factor':2}]
        allocator=Allocator(nodes);ticket=allocator.reserve_group('normal',normal,{'only':copies});dispatcher=Dispatcher(allocator,root/'out')
        try:
            result=dispatcher.execute(ticket,normal)
            print(json.dumps({'scenario':'normal','status':result['status'],'actual_rows':json.loads(Path(result['outputs']['only']).read_text()),'held_after':allocator.inspect()['held']}))
        finally:dispatcher.cleanup()
        members=[{'worker_id':'flex','allowed_nodes':['scarce','other'],'resources':{'cpu':1,'io':1},'factor':2},{'worker_id':'fixed','allowed_nodes':['scarce'],'resources':{'cpu':1,'io':1},'factor':3}]
        allocator=Allocator(nodes);ticket=allocator.reserve_group('trap',members,{'flex':copies,'fixed':{'scarce':copies['scarce']}})
        print(json.dumps({'scenario':'placement_direct_api','expected_assignment':{'flex':'other','fixed':'scarce'},'actual_ticket':ticket,'held_after_failed_plan':allocator.inspect()['held'],'processes_started':0}))
        allocator.release_group('trap')
        allocator=Allocator(nodes);ticket=allocator.reserve_group('changed',normal,{'only':copies})
        Path(copies['other']['path']).write_text(json.dumps([{'row_id':'r1','value':999}]))
        dispatcher=Dispatcher(allocator,root/'changed-out')
        try:
            result=dispatcher.execute(ticket,normal)
            print(json.dumps({'scenario':'execution_external_ticket_input_changed','expected_state':'failed_before_GO','expected_bound_digest':ticket['inputs']['only']['sha256'],'actual_input_digest':spec(Path(copies['other']['path']))['sha256'],'actual_status':result['status'],'actual_rows':json.loads(Path(result['outputs']['only']).read_text()),'log':result['log']}))
        finally:dispatcher.cleanup()
        print(json.dumps({'all_children_reaped':all(p.poll() is not None for p in dispatcher.children.values())}))
if __name__=='__main__':main()
