from __future__ import annotations

import pytest

from mimir import access_control
from mimir.client_file_resources import (
    ClientFileResourcePolicy,
    canonical_client_file_resource,
    client_file_resource_path,
)
from mimir.acp import execution_scope


def test_daemon_and_proxy_reuse_the_same_client_path_identity() -> None:
    assert access_control.canonical_client_file_resource is canonical_client_file_resource
    assert execution_scope.canonical_client_file_resource is canonical_client_file_resource
    assert access_control.client_file_resource_path is client_file_resource_path
    assert execution_scope.client_file_resource_path is client_file_resource_path
    assert access_control.ClientFileResourcePolicy is ClientFileResourcePolicy


@pytest.mark.parametrize(("path", "allowed"), [
    ("/workspace", True),
    ("/workspace/note", True),
    ("/workspace/../outside", False),
    ("/workspace-sibling/note", False),
])
def test_shared_cwd_policy_keeps_lexical_boundary(path: str, allowed: bool) -> None:
    resource = canonical_client_file_resource(path, cwd="/workspace")
    assert ClientFileResourcePolicy.for_cwd("/workspace").allows(resource) is allowed
