from receipt import result
from binding import identity,describe

def apply(db,plan,rows):
    out=[]
    with db:
        for n,row in enumerate(rows):
            old=db.execute('SELECT * FROM contacts WHERE external_id=? OR (tenant=? AND lower(email)=lower(?) AND active=1) ORDER BY id LIMIT 1',(row['external_id'],row['tenant'],row['email'])).fetchone()
            if old is None:
                c=db.execute('INSERT INTO contacts(tenant,email,active,external_id,n) VALUES(:tenant,:email,:active,:external_id,:n)',row);rid=c.lastrowid;action='insert'
            else:
                rid=old['id']
                if plan['action']['kind']=='nothing':action='skipped'
                else:db.execute('UPDATE contacts SET n=? WHERE id=?',(row['n'],rid));action='update'
            out.append(result(n,rid,action,db.execute('SELECT * FROM contacts WHERE id=?',(rid,)).fetchone()))
    ids=sorted({r['id'] for r in out})
    return {'receipts':out,'final_rows':[dict(db.execute('SELECT * FROM contacts WHERE id=?',(rid,)).fetchone()) for rid in ids]}
