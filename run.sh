#!/bin/bash
# Launcher for Wine Installer.
# Sets WINEPREFIX to a sandboxed location inside XDG_DATA_HOME so the
# Wine prefix stays isolated from any existing ~/.wine prefix.
export WINEPREFIX="${XDG_DATA_HOME:-$HOME/.local/share}/wineinstaller/prefix"
exec python3 /app/bin/repack_installer.py "$@"
