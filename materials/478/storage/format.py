"""Wire helpers shared with independent page producers; offsets are payload-relative."""
import json,struct,zlib
MAGIC=b'PCR2'
RECORD=struct.Struct('>HHH')
def split_page(raw):
    if raw[:4]!=MAGIC:raise ValueError('BAD_MAGIC')
    n=struct.unpack('>I',raw[4:8])[0]
    return json.loads(raw[8:8+n]),raw[8+n:]
def join_page(directory,payload):
    header=json.dumps(directory,sort_keys=True,separators=(',',':')).encode()
    return MAGIC+struct.pack('>I',len(header))+header+payload
def encode_group(rows):
    out=bytearray();previous=b''
    for key,value in rows:
        prefix=0
        while prefix<min(len(previous),len(key)) and previous[prefix]==key[prefix]:prefix+=1
        suffix=key[prefix:];out+=RECORD.pack(prefix,len(suffix),len(value))+suffix+value;previous=key
    return bytes(out)
def decode_group(payload):
    rows=[];at=0;previous=b''
    while at<len(payload):
        prefix,suffix_len,value_len=RECORD.unpack_from(payload,at);at+=RECORD.size
        suffix=payload[at:at+suffix_len];at+=suffix_len
        value=payload[at:at+value_len];at+=value_len
        key=previous[:prefix]+suffix;rows.append((key,value));previous=key
    return rows
