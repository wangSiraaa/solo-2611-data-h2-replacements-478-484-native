"""Offline request compiler used by preview and database import clients."""
from copy import deepcopy
from .catalog import identity
def bind(catalog,request):
    target=request['target']
    # Existing compiler treats any non-external target as active_email.
    index='external_id' if target.get('columns')==['external_id'] else 'active_email'
    return {'format':1,'catalog_sha256':identity(catalog),'target_index':index,'action':deepcopy(request['action'])}
