"""python -m civ6d  — run the daemon on 127.0.0.1:7790 (CIV6D_PORT to change)."""

import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "civ6-mcp" / "src"))

import uvicorn  # noqa: E402

(ROOT / "logs").mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(ROOT / "logs" / "civ6d.log"), logging.StreamHandler()],
)

if __name__ == "__main__":
    from civ6d.server import app

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("CIV6D_PORT", "7790")), log_level="warning")
