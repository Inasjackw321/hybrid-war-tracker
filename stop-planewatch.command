#!/bin/sh
# macOS / Linux: stop a Plane Watch that's running in the background.
cd "$(dirname "$0")" && exec python3 planewatch.py --stop
