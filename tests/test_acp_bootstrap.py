from __future__ import annotations
import json, os, subprocess, sys
from pathlib import Path
import pytest
from mimir.acp import bootstrap

def invoke(tmp_path, args, monkeypatch, capfd):
 monkeypatch.setenv('XDG_CONFIG_HOME',str(tmp_path)); code=bootstrap.main(args); out,err=capfd.readouterr(); return code,out,err
def test_profile_command_contract(tmp_path,monkeypatch,capfd):
 code,out,err=invoke(tmp_path,['profile','list'],monkeypatch,capfd); assert (code,out,err)==(0,'{"profiles":[],"version":1}\n','')
 code,out,err=invoke(tmp_path,['profile','set','default','--home','/tmp'],monkeypatch,capfd); assert code==0 and out=='' and err=='updated\n'
 code,out,err=invoke(tmp_path,['profile','show','default'],monkeypatch,capfd); assert code==0 and json.loads(out)['remote'] is None
 code,out,err=invoke(tmp_path,['profile','delete','default'],monkeypatch,capfd); assert (code,out,err)==(0,'','deleted\n')
def test_usage_and_help(tmp_path,monkeypatch,capfd):
 code,out,err=invoke(tmp_path,['profile'],monkeypatch,capfd); assert code==2 and not out and 'usage:' in err
 code,out,err=invoke(tmp_path,['--help'],monkeypatch,capfd); assert code==0 and 'usage:' in out
def test_module_extra_acp_is_usage_error(tmp_path):
 env={**os.environ,'XDG_CONFIG_HOME':str(tmp_path)}; p=subprocess.run([sys.executable,'-m','mimir.acp','acp'],capture_output=True,text=True,env=env); assert p.returncode==2
