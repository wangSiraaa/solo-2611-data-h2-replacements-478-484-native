import hashlib,json
COLUMNS={'id':'int64','tenant':'text','email':'nullable_text','active':'bool01','external_id':'text','n':'int64'}
def describe(db):
    return {'table':'contacts','columns':COLUMNS,'indexes':[{'name':r[0],'sql':r[1]} for r in db.execute("SELECT name,sql FROM sqlite_master WHERE type='index' AND tbl_name='contacts' ORDER BY name")]}
def identity(catalog):return hashlib.sha256(json.dumps(catalog,sort_keys=True,separators=(',',':')).encode()).hexdigest()
