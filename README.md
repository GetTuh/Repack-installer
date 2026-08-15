# Repack Installer

Install Windows game repacks (FitGirl, DODI, etc.) & more on Linux — pick a `setup.exe`, click Install, and the game shows up ready to play in [Heroic Games Launcher](https://heroicgameslauncher.com/).


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
- [Heroic Games Launcher](https://flathub.org/apps/com.heroicgameslauncher.hgl) (optional, but has nice integration)

 `umu-run` is downloaded automatically on first run if it's missing.
 
## How it works

Creates a clean Wine prefix and runs the installer through [umu-run](https://github.com/Open-Wine-Components/umu-launcher) + Proton, linking the install dir, so the other files aren't lost.

## Build from source

```bash
flatpak install --user flathub org.gnome.Platform//50 org.gnome.Sdk//50
flatpak run org.flatpak.Builder --user --install --force-clean build-dir io.github.GetTuh.RepackInstaller.yml
```
