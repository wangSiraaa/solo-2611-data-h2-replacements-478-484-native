"""The import path shares the public offline binding protocol."""
from binding import bind,describe
from execution import apply
def import_rows(db,request,rows):return apply(db,bind(describe(db),request),rows)
