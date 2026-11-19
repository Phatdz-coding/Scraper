from __future__ import annotations

import sys
from pathlib import Path


def load(name: str) -> str:
    """Load a JavaScript helper script from the package's scripts directory."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        meipass = Path(sys._MEIPASS)
        for candidate in (meipass / "scrape", meipass / "docscraper", meipass):
            script_path = candidate / "scripts" / name
            if script_path.is_file():
                return script_path.read_text(encoding="utf-8")
        base_dir = meipass / "scrape"
    else:
        base_dir = Path(__file__).resolve().parent

    return (base_dir / "scripts" / name).read_text(encoding="utf-8")