from __future__ import annotations
import pytest
from mimir.acp.credentials import SERVICE, CredentialError, CredentialMutationUncertain, NativeCredentialStore
class Backend:
 def __init__(self): self.value=None; self.fail=None; self.calls=[]
 def get_password(self,s,u): self.calls.append(('get',s,u)); return self.value
 def set_password(self,s,u,v): self.calls.append(('set',s,u,v)); self.value=v; (_ for _ in ()).throw(self.fail) if self.fail else None
 def delete_password(self,s,u): self.calls.append(('delete',s,u)); self.value=None; (_ for _ in ()).throw(self.fail) if self.fail else None
def test_crud_service_and_idempotence():
 b=Backend(); s=NativeCredentialStore(b); assert s.get('default') is None; s.set('default','key'); assert b.calls[-1]==('set',SERVICE,'default','key'); s.delete('default'); s.delete('default'); assert [c[0] for c in b.calls].count('delete')==1
def test_mutation_uncertain():
 b=Backend(); b.fail=RuntimeError('secret'); s=NativeCredentialStore(b)
 with pytest.raises(CredentialMutationUncertain): s.set('default','x')
def test_read_failure_definite():
 class Bad(Backend):
  def get_password(self,*a): raise RuntimeError('x')
 with pytest.raises(CredentialError,match='credential-read-failed'): NativeCredentialStore(Bad()).get('default')
