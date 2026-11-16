"""Native memory-image consumer: it does not start a WASM Runner."""
from abi import encode,decode_image
def consume(image,address,expected):return decode_image(image,address,expected)
if __name__=='__main__':
    import json
    raw=encode(['海',None,''],0,True,0)
    print(json.dumps(consume(raw,0,{'batch_no':0,'row_count':3,'final':True}),ensure_ascii=False))
