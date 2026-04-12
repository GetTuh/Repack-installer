# Repack Installer

A minimal GTK4 + libadwaita app for running Windows repack installers (FitGirl, DODI, etc.) on Linux via Wine.

Pick a `setup.exe`, click Install. The app translates the host path to a Wine `Z:\` path via `winepath` and launches it with an isolated `WINEPREFIX` so it never touches your main `~/.wine`.

## Requirements

- Wine (provided by the Flatpak base)
- Flatpak + Flathub

## Build

```bash
flatpak install --user flathub org.gnome.Platform//50 org.gnome.Sdk//50
flatpak install --user flathub org.winehq.Wine//stable-25.08
flatpak run org.flatpak.Builder --user --install --force-clean build-dir io.github.RepackInstaller.yml
```

## Run

```bash
flatpak run io.github.RepackInstaller
```

Wine prefix is stored at `~/.local/share/repackinstaller/prefix`, separate from any existing Wine setup.
