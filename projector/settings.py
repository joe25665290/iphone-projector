"""Small per-user settings file (%APPDATA%\\Projector\\settings.json)."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger("projector.settings")

PATH = Path(os.environ.get("APPDATA", Path.home())) / "Projector" / "settings.json"


def load() -> dict:
    try:
        return json.loads(PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception:  # noqa: BLE001 - a broken file shouldn't stop the app
        log.warning("設定檔讀取失敗，使用預設值: %s", PATH, exc_info=True)
        return {}


def save(values: dict) -> None:
    try:
        PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(values, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(PATH)
    except Exception:  # noqa: BLE001
        log.warning("設定檔寫入失敗: %s", PATH, exc_info=True)
