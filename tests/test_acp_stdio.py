from __future__ import annotations
import os
from mimir.acp.bootstrap import _reserve_stdout
def test_stdout_reservation_routes_python_output_to_stderr(capfd):
 saved=os.dup(1); previous=__import__('sys').stdout; frame=_reserve_stdout()
 try:
  print('diagnostic',flush=True); os.write(frame.fileno(),b'frame\n')
 finally:
  frame.close(); os.dup2(saved,1); os.close(saved); __import__('sys').stdout=previous
 out,err=capfd.readouterr(); assert out=='frame\n'; assert 'diagnostic' in err
