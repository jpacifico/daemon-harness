import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MEMORY_ROOT = Path(
    os.getenv("DAEMON_CORE_MEMORY_ROOT", str(PROJECT_ROOT / "memory"))
).expanduser().resolve()
