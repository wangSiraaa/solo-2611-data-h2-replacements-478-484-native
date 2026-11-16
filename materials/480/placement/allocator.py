"""Shared reservation API called by both the dispatcher and an independent planner client."""
class Allocator:
    def __init__(self,nodes):self.nodes={n:dict(c) for n,c in nodes.items()};self.held={}
    def available(self):
        return {n:{r:t-sum(h['resources'].get(r,0) for h in self.held.values() if h['node']==n) for r,t in c.items()} for n,c in self.nodes.items()}
    def reserve_group(self,stage,members,replicas):
        assignment={};inputs={}
        for member in members:
            chosen=None
            for node in member['allowed_nodes']:
                if node not in replicas[member['worker_id']]:continue
                if all(v<=self.available()[node][r] for r,v in member['resources'].items()):chosen=node;break
            if chosen is None:return None
            owner=stage+':'+member['worker_id'];self.held[owner]={'node':chosen,'resources':dict(member['resources'])}
            assignment[member['worker_id']]=chosen;inputs[member['worker_id']]=dict(replicas[member['worker_id']][chosen])
        return {'stage_id':stage,'assignment':assignment,'inputs':inputs}
    def release_group(self,stage):
        for owner in list(self.held):
            if owner.startswith(stage+':'):del self.held[owner]
    def inspect(self):return {'held':dict(self.held),'available':self.available()}
