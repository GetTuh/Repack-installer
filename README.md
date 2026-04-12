# Repack Installer

A minimal GTK4 + libadwaita app for running Windows repack installers (FitGirl, DODI, etc.) on Linux via [umu-run](https://github.com/Open-Wine-Components/umu-launcher) + Proton.

Pick a `setup.exe`, click Install. Automatically runs vcrun2022 via winetricks first, then launches the installer. Uses an isolated Wine prefix that won't touch your existing setup.

Designed for [Bazzite](https://bazzite.gg), where umu-run comes pre-installed.

## Install

Download `repack-installer.flatpak` from the [latest release](https://github.com/GetTuh/Linux-repack-installer/releases/latest) and run:

```bash
flatpak install --user repack-installer.flatpak
flatpak run io.github.GetTuh.RepackInstaller
```

## Requirements

- umu-run on the host (`umu-launcher`, pre-installed on Bazzite)
- Flatpak

## Build from source

```bash
flatpak install --user flathub org.gnome.Platform//50 org.gnome.Sdk//50
flatpak run org.flatpak.Builder --user --install --force-clean build-dir io.github.GetTuh.RepackInstaller.yml
```

## Wine prefix

Stored at `~/.local/share/repackinstaller/prefix`, separate from any existing Wine setup.
