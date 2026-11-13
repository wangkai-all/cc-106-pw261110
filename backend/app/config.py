"""Runtime configuration."""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.environ.get("APP_DATA_DIR", BASE_DIR / "data"))
UPLOAD_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "measurements.sqlite3"
FRONTEND_DIR = BASE_DIR / "frontend"

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Reject absurd uploads (4 GiB) but allow real measurement files.
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", 4 * 1024**3))
