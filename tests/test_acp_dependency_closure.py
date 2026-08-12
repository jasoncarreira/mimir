from __future__ import annotations
import importlib.util
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location("acp_dependency_closure",ROOT/".github"/"acp_dependency_closure.py"); assert spec and spec.loader
module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
def test_proxy_relay_and_client_closures_are_finite_and_runtime_blind(): module.assert_policy(module.module_paths(ROOT/"mimir"))
def test_entrypoint_acp_branch_has_only_the_client_dispatch():
 source=(ROOT/"mimir"/"entrypoint.py").read_text(); branch=source.split('if sys.argv[1:2] == ["acp"]:',1)[1].split('from mimir.cli',1)[0]; assert 'mimir.acp.bootstrap' in branch and 'mimir.cli' not in branch
def test_composition_is_globally_absent(): assert not (ROOT/"mimir"/"acp"/"composition.py").exists()
