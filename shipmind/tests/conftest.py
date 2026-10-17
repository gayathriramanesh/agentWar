import os
import sys
from pathlib import Path

os.environ.setdefault("SHIPMIND_MOCK_LLM", "true")      # tests never need an API key
os.environ.setdefault("SHIPMIND_MOCK_LATENCY_MS", "5")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
