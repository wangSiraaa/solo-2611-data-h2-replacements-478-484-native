import wasmtime
from abi import encode,decode_image
from product import Product
class Runner:
    def __init__(self):
        cfg=wasmtime.Config();cfg.consume_fuel=True;self.engine=wasmtime.Engine(cfg)
    def run(self,wat,batches,path,fuel=100000,job_id='job'):
        store=wasmtime.Store(self.engine);store.set_fuel(fuel)
        module=wasmtime.Module(self.engine,wat);instance=wasmtime.Instance(store,module,[]);exports=instance.exports(store)
        memory=exports['memory'];product=Product(path);product.begin(job_id,list(map(len,batches)));calls=[]
        try:
            for n,rows in enumerate(batches):
                final=n==len(batches)-1
                provisional=encode(rows,n,final,0);address=exports['alloc'](store,len(provisional))
                packed=encode(rows,n,final,address);memory.write(store,packed,address)
                result=exports['process'](store,address,n,int(final));calls.append(('process',n,result,memory.size(store)))
                batch=decode_image(bytes(memory.read(store)),result,{'batch_no':n,'row_count':len(rows),'final':final});product.append(job_id,batch)
                exports['release'](store,result);exports['dealloc'](store,address,len(packed));calls.append(('release',n))
            product.commit(job_id)
            return {'status':'complete','product':product.inspect(),'calls':calls}
        except Exception as exc:
            product.abort(job_id,type(exc).__name__)
            return {'status':'failed','reason':type(exc).__name__,'product':product.inspect(),'calls':calls}
