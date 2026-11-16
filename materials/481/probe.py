import tempfile,json,struct
from pathlib import Path
from runtime import Runner
from abi import encode
from batch_client import consume
from product import Product
from product_client import publish
with tempfile.TemporaryDirectory() as td:
    out={};batches=[['海',None,''],['salt']]
    for name in ('identity','bad_descriptor','second_trap','loop'):
        out[name]=Runner().run(Path(name+'.wat').read_text(),batches,str(Path(td)/(name+'.json')),job_id=name)
    out['native_normal']=consume(encode(['海',None,''],0,True,0),0,{'batch_no':0,'row_count':3,'final':True})
    raw=bytearray(encode(['海'],0,True,0));struct.pack_into('<I',raw,20,len(raw)+8)
    try:out['native_oob']={'accepted':consume(raw,0,{'batch_no':0,'row_count':1,'final':True})}
    except Exception as exc:out['native_oob']={'rejected':type(exc).__name__}
    out['product_normal']=publish(Path(td)/'native.json','native',[['x']])
    prod=Product(Path(td)/'partial.json');prod.begin('partial',[1,1]);prod.append('partial',{'batch_no':0,'rows':['first'],'final':False})
    try:out['partial_commit']={'returned':prod.commit('partial')}
    except Exception as exc:out['partial_commit']={'rejected':type(exc).__name__}
    out['partial_visible']=Product(Path(td)/'partial.json').inspect()
    print(json.dumps(out,ensure_ascii=False,sort_keys=True))
