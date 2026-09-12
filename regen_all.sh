#!/bin/bash
# Regenerates every flowgraphs/*.grc into its .py, then runs preflight.py.
# Run this after editing any .grc, or after add_satellite.py generates a
# new one - "forgot to grcc" has been a real, repeated source of confusion.
set -e

if ! command -v grcc &> /dev/null; then
    echo "grcc not found - install GNU Radio Companion first." >&2
    exit 1
fi

for grc in flowgraphs/*.grc; do
    echo "Generating ${grc%.grc}.py ..."
    grcc "$grc"
done

echo
echo "Running preflight checks..."
python3 preflight.py
