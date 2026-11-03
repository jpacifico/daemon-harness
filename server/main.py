import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

configured_env = os.getenv("DAEMON_CORE_ENV_FILE", "").strip()
load_dotenv(Path(configured_env).expanduser() if configured_env else ROOT / ".env")

from server.routes.health import router as health_router  # noqa: E402
from server.routes.openclaw import router as openclaw_router  # noqa: E402


app = FastAPI(
    title="Daemon Harness Canonical",
    description="Pont canonique local pour OpenClaw.",
    version="0.1.0",
)
app.include_router(health_router)
app.include_router(openclaw_router)
