import sqlite3

def create(path):
 db=sqlite3.connect(path);db.row_factory=sqlite3.Row
 db.executescript('CREATE TABLE contacts(id INTEGER PRIMARY KEY,tenant TEXT NOT NULL,email TEXT,active INTEGER NOT NULL,external_id TEXT UNIQUE,n INTEGER NOT NULL);CREATE UNIQUE INDEX active_email ON contacts(tenant,lower(email)) WHERE active=1;')
 return db
