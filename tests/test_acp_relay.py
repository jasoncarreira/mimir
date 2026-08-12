from pathlib import Path
import pytest
from mimir.acp.relay import RelayError, _socket
def test_relay_requires_absolute_home():
 with pytest.raises(RelayError): _socket(Path('relative'))
