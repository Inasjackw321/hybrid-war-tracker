"""Build dist/planewatch.pyz: the whole app in one file.

    python tools/build_app.py
    python dist/planewatch.pyz        # run it (Python 3.9+)

A .pyz is a zip that Python runs directly. It holds plane_watch/ and the
launcher, which installs matplotlib + airportsdata into ~/.planewatch/venv on
first run.
"""

import shutil
import sys
import tempfile
import zipapp
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def build(out: Path = ROOT / "dist" / "planewatch.pyz") -> Path:
    with tempfile.TemporaryDirectory() as d:
        shutil.copytree(ROOT / "plane_watch", Path(d) / "plane_watch", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copy(ROOT / "planewatch.py", Path(d) / "__main__.py")
        out.parent.mkdir(parents=True, exist_ok=True)
        zipapp.create_archive(d, out, interpreter="/usr/bin/env python3", compressed=True)
    return out


if __name__ == "__main__":
    path = build(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "dist" / "planewatch.pyz")
    print(f"built {path} ({path.stat().st_size // 1024} KB)")
