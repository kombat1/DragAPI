"""Каталог рабочих данных платформы: DRAGAPI_DATA_DIR или backend/data."""

import os
from pathlib import Path

DATA = Path(os.environ.get("DRAGAPI_DATA_DIR") or Path(__file__).resolve().parent.parent / "data").resolve()


def _apply_pending_restore():
    """Подготовленное восстановление из резервной копии применяется до того, как что-либо прочитает данные."""
    if not (DATA.with_name(DATA.name + ".restore") / ".restore-manifest.json").exists():
        return
    import sys
    from .backup import apply_pending
    previous = apply_pending(DATA)
    print(f"DragAPI: данные восстановлены из резервной копии"
          f"{f'; прежние данные — в {previous}' if previous else ''}", file=sys.stderr)


_apply_pending_restore()
