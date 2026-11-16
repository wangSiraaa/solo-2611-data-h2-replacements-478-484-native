"""Public batch product writer shared by native and WASM producers."""
import json
from pathlib import Path
class Product:
    def __init__(self,path):self.path=Path(path);self.job_id=None;self.rows=[];self.receipts=[]
    def begin(self,job_id,row_counts):
        self.job_id=job_id;self.row_counts=list(row_counts);self.rows=[];self.receipts=[]
    def append(self,job_id,batch):
        self.rows.extend(batch['rows']);self.receipts.append({'batch_no':batch['batch_no'],'final':batch['final'],'row_count':len(batch['rows'])})
        self.path.write_text(json.dumps({'status':'complete','job_id':job_id,'rows':self.rows,'batches':self.receipts},ensure_ascii=False))
    def commit(self,job_id):return self.inspect()
    def abort(self,job_id,reason):return {'status':'failed','reason':reason}
    def inspect(self):return json.loads(self.path.read_text()) if self.path.exists() else {'status':'unpublished'}
