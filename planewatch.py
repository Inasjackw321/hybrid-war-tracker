#!/usr/bin/env python3
"""Plane Watch: one command, everything.

    python planewatch.py            (or: python planewatch.pyz)

Watches the ADS-B feeds for every emergency squawk (7500 hijack, 7600 radio
failure, 7700 emergency, 7400 UAV lost link, 7777 intercept) and military
emergencies, scans ACARS for bomb threats, diversions, medical, security and
other trouble, renders a card for each aircraft and opens a live dashboard.

The first run installs what it needs (matplotlib, airportsdata) into a private
folder (~/.planewatch/venv), so nothing on your system is changed.
Options: python planewatch.py --help
"""

import os
import subprocess
import sys
from pathlib import Path

REQUIREMENTS = ["matplotlib>=3.8", "airportsdata>=20240101"]
HOME = Path(os.environ.get("PLANEWATCH_HOME") or Path.home() / ".planewatch")


def _have_deps() -> bool:
    try:
        import airportsdata  # noqa: F401
        import matplotlib  # noqa: F401
        import numpy  # noqa: F401
    except ImportError:
        return False
    return True


def _bootstrap():
    """Install the dependencies into a private virtualenv and re-run this file with it."""
    if os.environ.get("PLANEWATCH_BOOTSTRAPPED"):
        sys.exit("Plane Watch: the dependencies still can't be imported after installing them. "
                 f"Try deleting {HOME / 'venv'} and running again.")
    venv = HOME / "venv"
    py = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    marker = venv / ".planewatch-deps"
    if not py.exists():
        print("Plane Watch: first run, setting up (about a minute, only once)…", flush=True)
        try:
            import venv as venv_mod

            venv_mod.create(venv, with_pip=True)
        except Exception as e:  # noqa: BLE001
            sys.exit(f"Plane Watch: couldn't create a virtual environment ({e}).\n"
                     "On Debian/Ubuntu install it with:  sudo apt install python3-venv\n"
                     f"Or install the packages yourself:  pip install {' '.join(REQUIREMENTS)}")
    if not marker.exists() or marker.read_text() != "\n".join(REQUIREMENTS):
        print("Plane Watch: installing matplotlib and airportsdata…", flush=True)
        r = subprocess.call([str(py), "-m", "pip", "install", "-q", "--disable-pip-version-check", *REQUIREMENTS])
        if r != 0:
            sys.exit("Plane Watch: installing the dependencies failed (see the pip output above).")
        marker.write_text("\n".join(REQUIREMENTS))
    env = dict(os.environ, PLANEWATCH_BOOTSTRAPPED="1")
    sys.exit(subprocess.call([str(py), os.path.abspath(sys.argv[0]), *sys.argv[1:]], env=env))


def main():
    if sys.version_info < (3, 9):
        sys.exit("Plane Watch needs Python 3.9 or newer.")
    if not _have_deps():
        _bootstrap()
    from plane_watch.__main__ import main as cli

    sys.exit(cli(sys.argv[1:]))


if __name__ == "__main__":
    main()
