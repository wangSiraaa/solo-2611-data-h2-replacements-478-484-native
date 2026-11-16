"""Offline consumer. A catalog JSON and request can be bound without a DB."""
from binding import bind
def preview(catalog,request):return bind(catalog,request)
if __name__=='__main__':
    import json,sys
    print(json.dumps(preview(json.load(open(sys.argv[1])),json.load(open(sys.argv[2]))),sort_keys=True))
