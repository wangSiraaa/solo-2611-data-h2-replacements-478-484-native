"""Existing page producer. Ordinary create/lookup works; edits use stale directory spans."""
import zlib
from .format import split_page,join_page,encode_group,decode_group
class PageWriter:
    def create(self,rows,restart=4):
        rows=sorted(rows);groups=[rows[i:i+restart] for i in range(0,len(rows),restart)]
        directory=[];payload=bytearray()
        for group in groups:
            encoded=encode_group(group)
            directory.append(dict(first=group[0][0].hex(),offset=len(payload),length=len(encoded),count=len(group),crc32=zlib.crc32(encoded)))
            payload+=encoded
        return join_page(directory,bytes(payload))
    def put(self,raw,key,value):
        directory,payload=split_page(raw)
        index=max((i for i,e in enumerate(directory) if bytes.fromhex(e['first'])<=key),default=0)
        e=directory[index];rows=decode_group(payload[e['offset']:e['offset']+e['length']])
        values=dict(rows);values[key]=value;encoded=encode_group(sorted(values.items()))
        start,end=e['offset'],e['offset']+e['length']
        # Original local replacement leaves following segment offsets unchanged.
        payload=payload[:start]+encoded+payload[end:]
        e.update(first=min(values).hex(),length=len(encoded),count=len(values),crc32=zlib.crc32(encoded))
        return join_page(directory,payload)
