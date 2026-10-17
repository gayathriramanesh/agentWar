import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("SHIPMIND_MOCK_LLM", "true")      # tests never need an API key
os.environ.setdefault("SHIPMIND_MOCK_LATENCY_MS", "5")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# Isolated shipment store per test session: duplicate-order detection must not see previous runs.
os.environ["SHIPMIND_STATE_DIR"] = tempfile.mkdtemp(prefix="shipmind-test-")


import copy  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_world():
    """Every test starts from the same inventory, closed breakers, empty quote cache and no chaos."""
    from core import data, logistics
    from core.carrier_apis import Chaos
    from core.healing import BREAKER

    inventory = copy.deepcopy(data.INVENTORY)
    Chaos.configure(0.0, 42)
    BREAKER.reset()
    logistics.clear_quote_cache()
    yield
    data.INVENTORY.clear()
    data.INVENTORY.update(inventory)
    Chaos.configure(0.0, 42)
    BREAKER.reset()
