#!/usr/bin/env bash
set -euo pipefail

sudo pacman -S --needed python pyside6 pacman-contrib

cat <<'EOF'
System dependencies installed.

Run the app from this folder with:
  python3 app.py
EOF
