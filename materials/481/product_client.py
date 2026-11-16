"""Native producer using the same Product protocol without WASM."""
from product import Product
def publish(path,job_id,batches):
    p=Product(path);p.begin(job_id,[len(x) for x in batches])
    for n,rows in enumerate(batches):p.append(job_id,{'batch_no':n,'rows':rows,'final':n==len(batches)-1})
    p.commit(job_id);return p.inspect()
if __name__=='__main__':
    import tempfile,json
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:print(json.dumps(publish(Path(td)/'p.json','native',[['海',None,'']]),ensure_ascii=False))
