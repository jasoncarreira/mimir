from __future__ import annotations
import json, os
from pathlib import Path
import pytest
from mimir.acp.profiles import Profile, ProfileError, ProfileStore, RemoteProfile, selected_profile

def store(tmp_path):
 p=tmp_path/'mimir'/'acp'/'profiles.json'; return ProfileStore(p)
def test_profile_roundtrip_and_canonical(tmp_path):
 s=store(tmp_path); p=Profile('default',Path('/srv/mimir')); s.set(p)
 assert s.get('default')==p; assert s.path.read_bytes()==b'{"profiles":{"default":{"home":"/srv/mimir","remote":null}},"version":1}\n'
 assert s.path.stat().st_mode&0o777==0o600
def test_remote_and_selection(tmp_path):
 r=RemoteProfile('example.com','user',22,Path('/id'),Path('/known')); p=Profile('z',Path('/home'),r); s=store(tmp_path); s.set(p); assert s.get('z')==p
 assert selected_profile(None,{'MIMIR_ACP_PROFILE':' z '})=='z'; assert selected_profile(None,{})=='default'
def test_duplicate_unknown_version_and_unsafe(tmp_path):
 s=store(tmp_path); s.path.parent.mkdir(parents=True,mode=0o700); os.chmod(s.path.parent.parent,0o700); os.chmod(s.path.parent,0o700)
 s.path.write_text('{"version":1,"version":1,"profiles":{}}'); os.chmod(s.path,0o600)
 with pytest.raises(ProfileError,match='invalid-profile-store'): s.list()
 s.path.write_text('{"version":2,"profiles":{}}')
 with pytest.raises(ProfileError,match='unsupported-profile-version'): s.list()
 os.chmod(s.path,0o644)
 with pytest.raises(ProfileError,match='unsafe-profile-store'): s.list()
def test_delete_missing_and_empty(tmp_path):
 s=store(tmp_path)
 with pytest.raises(ProfileError,match='profile-not-found'): s.delete('default')
 s.set(Profile('default',Path('/x'))); s.delete('default'); assert json.loads(s.path.read_text())=={'profiles':{},'version':1}
