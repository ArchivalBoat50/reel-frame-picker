#!/bin/bash
# ── Reel Pipeline Setup & Launch ──────────────────────────────────
# Run this once to install deps, then use it to launch the app.

set -e

echo ""
echo "  ◆ Reel Pipeline"
echo "  ─────────────────────────────────"
echo ""

# Check Python
if ! command -v python3 &> /dev/null; then
    echo "  ✗ Python 3 not found. Install from https://python.org"
    exit 1
fi
echo "  ✓ Python 3 found"

# Check/install ffmpeg
if ! command -v ffmpeg &> /dev/null; then
    echo "  → Installing ffmpeg..."
    if command -v brew &> /dev/null; then
        brew install ffmpeg
    else
        echo "  ✗ ffmpeg not found. Install with: brew install ffmpeg"
        exit 1
    fi
fi
echo "  ✓ ffmpeg ready"

# Check/install Flask & requests
if ! python3 -c "import flask" 2>/dev/null; then
    echo "  → Installing Flask..."
    pip3 install flask
fi
echo "  ✓ Flask ready"

if ! python3 -c "import requests" 2>/dev/null; then
    echo "  → Installing requests..."
    pip3 install requests
fi
echo "  ✓ requests ready"

echo ""
echo "  All dependencies satisfied."
echo "  Starting Reel Pipeline..."
echo ""

# Launch
cd "$(dirname "$0")"
python3 app.py "$@"
