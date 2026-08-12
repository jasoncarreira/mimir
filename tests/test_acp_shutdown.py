from __future__ import annotations
import asyncio, io
import pytest
from mimir.acp.host import _FrameDelivery
@pytest.mark.asyncio
async def test_frame_delivery_is_bounded_and_terminal():
 stream=io.BytesIO(); errors=[]; delivery=_FrameDelivery(stream,16,errors.append); assert delivery.write(b'frame')==5; delivery.finish(); await delivery.wait_terminal(); delivery.join(); assert stream.getvalue()==b'frame'; assert not errors
@pytest.mark.asyncio
async def test_frame_delivery_rejects_capacity():
 delivery=_FrameDelivery(io.BytesIO(),2,lambda e:None)
 with pytest.raises(BufferError): delivery.write(b'long')
 with pytest.raises(BufferError): await delivery.wait_terminal()
 delivery.join()
