"""Каталог рабочих данных платформы: DRAGAPI_DATA_DIR или backend/data."""

import os
from pathlib import Path

DATA = Path(os.environ.get("DRAGAPI_DATA_DIR") or Path(__file__).resolve().parent.parent / "data").resolve()
