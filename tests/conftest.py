import os
import tempfile
from pathlib import Path

# Unit tests are deterministic and must never emit traces or network traffic.
os.environ["LANGSMITH_TRACING"] = "false"
_TEST_STATE = Path(tempfile.mkdtemp(prefix="opsgraph-tests-")) / "state.db"
os.environ["OPSGRAPH_STATE_PATH"] = str(_TEST_STATE)
