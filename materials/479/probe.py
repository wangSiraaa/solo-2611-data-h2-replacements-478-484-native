import json,sqlite3,tempfile
from pathlib import Path
from transactions.manager import Transactions
from exports.writer import DatasetWriter
def conn(path):return sqlite3.connect(path,isolation_level=None)
def main():
    with tempfile.TemporaryDirectory() as d:
        root=Path(d);db=conn(root/'work.db');tx=Transactions(db);writer=DatasetWriter(db,tx,root/'out')
        with tx.scope():writer.put('a','sales',7);writer.request_export('normal','sales')
        writer.flush();print(json.dumps({'scenario':'normal','file_exists':(root/'out/normal.json').exists(),'rows':json.loads((root/'out/normal.json').read_text())['rows']}))
        notices=[]
        try:
            with tx.scope():
                with tx.scope():
                    db.execute("INSERT INTO facts VALUES('orphan','sales',9)")
                    tx.before_commit(lambda:notices.append('prepare-child'))
                    tx.on_commit(lambda:notices.append('committed-child'))
                raise ValueError('outer abort')
        except ValueError:pass
        print(json.dumps({'scenario':'direct_sql_nested_callbacks','expected_notices':[],'actual_notices':notices,'orphan_rows':db.execute("SELECT * FROM facts WHERE row_id='orphan'").fetchall(),'tx_events':tx.inspect()}))
        with tx.scope():writer.put('b','sales',11);writer.request_export('reopen','sales')
        db.close();db=conn(root/'work.db');tx2=Transactions(db);writer2=DatasetWriter(db,tx2,root/'out');writer2.flush()
        print(json.dumps({'scenario':'export_reopen','committed_rows':db.execute('SELECT row_id,value FROM facts ORDER BY row_id').fetchall(),'expected_reopen_export':True,'actual_reopen_export':(root/'out/reopen.json').exists(),'reopened_queue':writer2.inspect()}))
        db.execute('PRAGMA foreign_keys=ON')
        db.executescript('CREATE TABLE parent(id INTEGER PRIMARY KEY); CREATE TABLE child(pid INTEGER REFERENCES parent(id) DEFERRABLE INITIALLY DEFERRED);')
        tx3=Transactions(db);notices=[]
        try:
            with tx3.scope():
                db.execute('INSERT INTO child VALUES(99)');tx3.on_commit(lambda:notices.append('committed'))
        except sqlite3.IntegrityError:pass
        print(json.dumps({'scenario':'sqlite_commit_rejected','expected_in_transaction':False,'actual_in_transaction':db.in_transaction,'actual_notifications':notices}))
        if db.in_transaction:db.rollback()
        tx4=Transactions(db);notices=[]
        def fail_notice():notices.append('first');raise RuntimeError('notification fails')
        try:
            with tx4.scope():
                db.execute("INSERT INTO facts VALUES('committed-notice','sales',13)")
                tx4.on_commit(fail_notice);tx4.on_commit(lambda:notices.append('second'))
        except RuntimeError:pass
        print(json.dumps({'scenario':'post_commit_notification_failed','committed_fact_rows':db.execute("SELECT row_id,value FROM facts WHERE row_id='committed-notice'").fetchall(),'expected_notices':['first','second'],'actual_notices':notices,'events':tx4.inspect()}))
        db.close()
if __name__=='__main__':main()
