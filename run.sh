#!/bin/bash
export WINEPREFIX="$HOME/.local/share/repackinstaller/prefix"
exec python3 /app/bin/repack_installer.py "$@"
