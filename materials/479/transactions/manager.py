from contextlib import contextmanager
class Transactions:
    """Shared API also used by direct SQL transformations with no DatasetWriter."""
    def __init__(self,connection):self.connection=connection;self.scopes=[];self.events=[]
    @contextmanager
    def scope(self):
        depth=len(self.scopes);name='scope_'+str(depth);callbacks={'prepare':[],'committed':[]}
        self.connection.execute('BEGIN' if depth==0 else 'SAVEPOINT '+name);self.scopes.append(callbacks)
        try:
            yield self
            for callback in callbacks['prepare']:callback()
        except Exception:
            self.connection.execute('ROLLBACK' if depth==0 else 'ROLLBACK TO '+name)
            if depth:self.connection.execute('RELEASE '+name)
            self.events.append({'depth':depth,'state':'rolled_back'});raise
        else:
            self.connection.execute('COMMIT' if depth==0 else 'RELEASE '+name)
            self.events.append({'depth':depth,'state':'committed'})
            for callback in callbacks['committed']:callback()
        finally:self.scopes.pop()
    def before_commit(self,callback):self.scopes[-1]['prepare'].append(callback)
    def on_commit(self,callback):self.scopes[-1]['committed'].append(callback)
    def inspect(self):return list(self.events)
