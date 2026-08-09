# Repack Installer

Install Windows game repacks (FitGirl, DODI, etc.) on Linux — pick a `setup.exe`, click Install, and the game shows up ready to play in [Heroic Games Launcher](https://heroicgameslauncher.com/).

## How it works

1. Pick your `setup.exe`
2. Click **Install** — it creates a clean Wine prefix and runs the installer through [umu-run](https://github.com/Open-Wine-Components/umu-launcher) + Proton
3. Click **Add to Heroic** — the game appears in Heroic with cover art, ready to launch

No manual Wine prefix setup, no hunting through folders for the right `.exe`.

## Install

```bash
flatpak remote-add --if-not-exists --user repack-installer https://gettuh.github.io/Repack-installer/repackinstaller.flatpakrepo
flatpak install --user repack-installer io.github.GetTuh.RepackInstaller
```

Update anytime with:

```bash
flatpak update --user
```

## Requirements

- Flatpak
- [Heroic Games Launcher](https://flathub.org/apps/com.heroicgameslauncher.hgl) (optional — that's where installed games show up)

That's it. `umu-run` is downloaded automatically on first run if it's missing — no extra setup.

## Build from source

```bash
flatpak install --user flathub org.gnome.Platform//50 org.gnome.Sdk//50
flatpak run org.flatpak.Builder --user --install --force-clean build-dir io.github.GetTuh.RepackInstaller.yml
```
