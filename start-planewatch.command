#!/bin/sh
# macOS / Linux: double-click (or run) to start Plane Watch in the background.
cd "$(dirname "$0")" && exec python3 planewatch.py --background "$@"
