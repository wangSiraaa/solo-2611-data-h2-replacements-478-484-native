import json
from pathlib import Path
class DatasetWriter:
    def __init__(self,connection,transactions,directory):
        self.connection=connection;self.transactions=transactions;self.directory=Path(directory);self.directory.mkdir(parents=True,exist_ok=True);self.pending={}
        connection.execute('CREATE TABLE IF NOT EXISTS facts(row_id TEXT PRIMARY KEY,dataset TEXT,value INTEGER)')
    def put(self,row_id,dataset,value):
        self.connection.execute('INSERT INTO facts VALUES(?,?,?) ON CONFLICT(row_id) DO UPDATE SET dataset=excluded.dataset,value=excluded.value',(row_id,dataset,value))
    def remove(self,row_id):self.connection.execute('DELETE FROM facts WHERE row_id=?',(row_id,))
    def request_export(self,export_id,dataset):
        request={'dataset':dataset,'status':'requested'};self.pending[export_id]=request
        def freeze():
            request['rows']=self.connection.execute('SELECT row_id,value FROM facts WHERE dataset=? ORDER BY row_id',(dataset,)).fetchall()
        def eligible():request['status']='pending'
        self.transactions.before_commit(freeze);self.transactions.on_commit(eligible)
    def flush(self):
        for export_id,request in self.pending.items():
            if request['status']!='pending':continue
            (self.directory/(export_id+'.json')).write_text(json.dumps(request));request['status']='complete'
        return self.inspect()
    def inspect(self):return self.pending.copy()
