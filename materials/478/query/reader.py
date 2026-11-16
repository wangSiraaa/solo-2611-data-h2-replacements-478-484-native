"""Page consumer used without PageWriter by the reporting application."""
from storage.format import split_page,decode_group
class PageReader:
    def __init__(self,raw):self.directory,self.payload=split_page(raw);self.decoded=0;self.groups_read=[]
    def _group(self,i):
        e=self.directory[i];rows=decode_group(self.payload[e['offset']:e['offset']+e['length']]);self.decoded+=len(rows);self.groups_read.append(i);return rows
    def _locate(self,key):return max((i for i,e in enumerate(self.directory) if bytes.fromhex(e['first'])<=key),default=0)
    def get(self,key):
        if not self.directory:return None
        return dict(self._group(self._locate(key))).get(key)
    def range(self,start,end):
        if not self.directory:return []
        # This consumer was written for a range contained in one restart group.
        return [(k,v) for k,v in self._group(self._locate(start)) if start<=k<end]
    def inspect(self):return dict(directory=self.directory,groups_read=self.groups_read,decoded=self.decoded)
