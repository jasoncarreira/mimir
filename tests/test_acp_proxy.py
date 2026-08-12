from __future__ import annotations
import json
import pytest
from mimir.acp.proxy import FrameWriter, ProxyError
class Writer:
 def __init__(self): self.data=b''
 def write(self,d): self.data+=d
 async def drain(self): pass
 def close(self): pass
 def is_closing(self): return False
 async def wait_closed(self): pass
@pytest.mark.asyncio
async def test_authenticate_overwrites_reserved_metadata():
 w=Writer(); t=FrameWriter(w,'raw-key'); t.write(json.dumps({'method':'authenticate','params':{'methodId':'mimir-web-key','_meta':{'ok':1,'mimir':'x','mimir.fake':'x'}}}).encode()+b'\n'); await t.drain()
 m=json.loads(w.data); assert m['params']['_meta']=={'ok':1,'mimir.webKey':'raw-key'}
def test_malformed_and_oversize_are_generic():
 w=Writer(); t=FrameWriter(w,'SECRET')
 with pytest.raises(ProxyError,match='invalid frame'): t.write(b'not-json\n')
 assert b'SECRET' not in w.data
