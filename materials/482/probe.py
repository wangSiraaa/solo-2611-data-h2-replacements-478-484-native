import tempfile,json
from database import create
from binding import bind,describe
from execution import apply
from importer import import_rows
from preview import preview

def row(email,external,n):return dict(tenant='t',email=email,active=1,external_id=external,n=n)
request={'target':{'columns':['tenant',{'expr':'lower','column':'email'}],'where':{'column':'active','op':'eq','value':1}},'action':{'kind':'update','set':{'n':{'excluded':'n'}}}}
with tempfile.TemporaryDirectory() as td:
    db=create(td+'/c.sqlite');catalog=describe(db)
    normal=import_rows(db,request,[row('a@x','x1',1),row('A@X','x2',2)])
    bad={'target':{'columns':['tenant',{'expr':'lower','column':'email'}]},'action':request['action']}
    try:invalid={'accepted':preview(catalog,bad)}
    except Exception as exc:invalid={'rejected':type(exc).__name__,'message':str(exc)}
    plan=bind(catalog,request);roundtrip=json.loads(json.dumps(plan))
    try:wrong={'returned':apply(db,roundtrip,[row('b@x','x1',9)])}
    except Exception as exc:wrong={'rejected':type(exc).__name__}
    print(json.dumps({'normal':normal,'offline_missing_predicate':invalid,'valid_plan_non_target_conflict':wrong,'after':[dict(x) for x in db.execute('SELECT * FROM contacts ORDER BY id')]},sort_keys=True))
    db.close()
