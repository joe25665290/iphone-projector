"""Double-click to start Projector without a console window.

Windows opens .pyw files with pythonw (no console). This file only starts app.py with the
project's own environment (.venv), which has the packages Projector needs.
For live log output in a console, use Projector-debug.bat instead.
"""

import ctypes
import subprocess
import sys
from pathlib import Path

here = Path(__file__).resolve().parent
pythonw = here / ".venv" / "Scripts" / "pythonw.exe"

if not pythonw.exists():
    ctypes.windll.user32.MessageBoxW(
        None, "找不到 .venv。請先依照 README 的「安裝」步驟安裝 Python 套件。", "Projector", 0x10)
    sys.exit(1)

subprocess.Popen([str(pythonw), str(here / "app.py"), *sys.argv[1:]], cwd=here)
