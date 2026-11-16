import json,zlib,struct
from storage.writer import PageWriter
from storage.format import encode_group,join_page,split_page
from query.reader import PageReader
# The reader fixture is authored directly from the wire contract, not via PageWriter.
def independent_page():
    groups=[[(b'aa0',b'0'),(b'aa1',b'1')],[(b'bb0',b'2'),(b'bb1',b'3')]]
    entries=[];payload=bytearray()
    for group in groups:
        part=b''.join(struct.pack('>HHH',0,len(k),len(v))+k+v for k,v in group);entries.append(dict(first=group[0][0].hex(),offset=len(payload),length=len(part),count=len(group),crc32=zlib.crc32(part)));payload+=part
    header=json.dumps(entries,sort_keys=True,separators=(',',':')).encode()
    return b'PCR2'+struct.pack('>I',len(header))+header+payload
def main():
    writer=PageWriter();rows=[(b'aa0',b'0'),(b'aa1',b'1'),(b'bb0',b'2'),(b'bb1',b'3')]
    raw=writer.create(rows,restart=2)
    print(json.dumps({'scenario':'normal','get_aa1':PageReader(raw).get(b'aa1').decode(),'range_same_group':[(k.decode(),v.decode()) for k,v in PageReader(raw).range(b'aa0',b'aa9')]}))
    reader=PageReader(independent_page());actual=reader.range(b'aa1',b'bb9')
    print(json.dumps({'scenario':'external_reader_cross_group','expected_keys':['aa1','bb0','bb1'],'actual_keys':[k.decode() for k,v in actual],'reader':reader.inspect()}))
    changed=writer.put(raw,b'aa0',b'longer value')
    d,p=split_page(changed);e=d[1];part=p[e['offset']:e['offset']+e['length']]
    try:value=PageReader(changed).get(b'bb0');result=None if value is None else value.hex()
    except Exception as exc:result=type(exc).__name__+':'+str(exc)
    print(json.dumps({'scenario':'writer_growth','expected_bb0':'32','actual_bb0':result,'second_group_crc_matches':zlib.crc32(part)==e['crc32'],'serialized_bytes':len(changed)}))
    benchmark=writer.create([(f'k{i:03}'.encode(),b'v') for i in range(64)],restart=8)
    print(json.dumps({'scenario':'capacity_witness','records':64,'bytes':len(benchmark),'fits_4096':len(benchmark)<=4096}))
if __name__=='__main__':main()
