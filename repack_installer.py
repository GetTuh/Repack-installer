#!/usr/bin/env python3
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, GLib, Gio

import os
import sys
import subprocess
import threading

APP_ID = 'io.github.GetTuh.RepackInstaller'
WINEPREFIX = os.path.join(os.path.expanduser('~'), '.local', 'share',
                          'repackinstaller', 'prefix')


def _umu_cmd(*args, exe_dir=None):
    cmd = [
        'flatpak-spawn', '--host',
        f'--env=WINEPREFIX={WINEPREFIX}',
        '--env=GAMEID=0',
        '--env=UMU_RUNTIME_UPDATE=0',
    ]
    if exe_dir:
        cmd += [f'--directory={exe_dir}']
    cmd += ['umu-run'] + list(args)
    return cmd


class WineInstallerWindow(Adw.ApplicationWindow):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.set_title('Repack Installer')
        self.set_default_size(500, 340)
        self._exe_path = None
        self._proc = None
        self._build_ui()
        threading.Thread(target=self._check_umu, daemon=True).start()

    def _build_ui(self):
        toolbar_view = Adw.ToolbarView()
        self.set_content(toolbar_view)

        header = Adw.HeaderBar()
        toolbar_view.add_top_bar(header)

        clamp = Adw.Clamp(maximum_size=600)
        toolbar_view.set_content(clamp)

        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        main_box.set_margin_top(24)
        main_box.set_margin_bottom(24)
        main_box.set_margin_start(24)
        main_box.set_margin_end(24)
        clamp.set_child(main_box)

        file_group = Adw.PreferencesGroup(title='Setup Executable')
        main_box.append(file_group)

        self.file_row = Adw.ActionRow(
            title='No file selected',
            subtitle='Choose a Windows installer (.exe)'
        )
        self.browse_btn = Gtk.Button(label='Browse…')
        self.browse_btn.add_css_class('suggested-action')
        self.browse_btn.set_valign(Gtk.Align.CENTER)
        self.browse_btn.set_sensitive(False)
        self.browse_btn.connect('clicked', self._on_browse)
        self.file_row.add_suffix(self.browse_btn)
        file_group.add(self.file_row)

        status_group = Adw.PreferencesGroup(title='Status')
        main_box.append(status_group)

        self.status_row = Adw.ActionRow(
            title='Checking for umu-run…',
            subtitle='Please wait'
        )
        self.spinner = Gtk.Spinner()
        self.spinner.set_valign(Gtk.Align.CENTER)
        self.spinner.start()
        self.status_row.add_prefix(self.spinner)
        status_group.add(self.status_row)

        self.install_btn = Gtk.Button(label='Install')
        self.install_btn.add_css_class('suggested-action')
        self.install_btn.add_css_class('pill')
        self.install_btn.set_halign(Gtk.Align.CENTER)
        self.install_btn.set_sensitive(False)
        self.install_btn.connect('clicked', self._on_install)
        main_box.append(self.install_btn)

    def _check_umu(self):
        result = subprocess.run(
            ['flatpak-spawn', '--host', 'sh', '-c', 'command -v umu-run'],
            capture_output=True
        )
        if result.returncode == 0:
            self._idle(self._set_status, 'Ready', 'Select a setup.exe to get started', False)
            self._idle(self.browse_btn.set_sensitive, True)
        else:
            self._idle(self._set_status, 'umu-run not found',
                       'Install umu-launcher on the host system (pre-installed on Bazzite)', False)

    def _on_browse(self, _btn):
        exe_filter = Gtk.FileFilter()
        exe_filter.set_name('Windows Executables (*.exe)')
        exe_filter.add_pattern('*.exe')
        exe_filter.add_pattern('*.EXE')

        all_filter = Gtk.FileFilter()
        all_filter.set_name('All files')
        all_filter.add_pattern('*')

        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(exe_filter)
        filters.append(all_filter)

        dialog = Gtk.FileDialog(title='Select Setup Executable')
        dialog.set_filters(filters)
        dialog.set_default_filter(exe_filter)
        dialog.open(self, None, self._on_file_chosen)

    def _on_file_chosen(self, dialog, result):
        try:
            file = dialog.open_finish(result)
        except GLib.Error:
            return
        if file:
            self._exe_path = file.get_path()
            name = os.path.basename(self._exe_path)
            self.file_row.set_title(name)
            self.file_row.set_subtitle(self._exe_path)
            self.install_btn.set_sensitive(True)
            self._set_status('Ready', f'Selected: {name}', False)

    def _on_install(self, _btn):
        if not self._exe_path:
            return
        self.install_btn.set_sensitive(False)
        self.browse_btn.set_sensitive(False)
        self._set_status('Starting…', 'Launching umu-run…', True)
        threading.Thread(target=self._run_install, daemon=True).start()

    def _run_install(self):
        exe_dir = os.path.dirname(self._exe_path)

        self._idle(self._set_status, 'Installing vcrun2022…', 'winetricks vcrun2022', True)
        if not self._run_cmd(_umu_cmd('winetricks', 'vcrun2022'), label='winetricks'):
            return

        self._idle(self._set_status, 'Running installer…', self._exe_path, True)
        self._run_cmd(_umu_cmd(self._exe_path, exe_dir=exe_dir), label='umu-run')

        self._idle(self._set_status, 'Done', 'Installer finished.', False)
        self._idle(self.install_btn.set_sensitive, True)
        self._idle(self.browse_btn.set_sensitive, True)

    def _run_cmd(self, cmd, label):
        print(f'[repack-installer] {label}: {cmd}', flush=True)
        try:
            self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                          stderr=subprocess.STDOUT)
            for line in self._proc.stdout:
                print(f'[{label}] {line.decode(errors="replace").rstrip()}', flush=True)
            rc = self._proc.wait()
            print(f'[repack-installer] {label} exited rc={rc}', flush=True)
        except Exception as e:
            self._idle(self._set_status, 'Error', str(e)[:200], False)
            self._idle(self.install_btn.set_sensitive, True)
            self._idle(self.browse_btn.set_sensitive, True)
            return False
        finally:
            self._proc = None
        return True

    @staticmethod
    def _idle(fn, *args):
        GLib.idle_add(fn, *args)

    def _set_status(self, title, subtitle, spinning):
        self.status_row.set_title(title)
        self.status_row.set_subtitle(subtitle)
        if spinning:
            self.spinner.start()
        else:
            self.spinner.stop()
        return GLib.SOURCE_REMOVE


class RepackInstallerApp(Adw.Application):
    def __init__(self):
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS
        )
        self.connect('activate', self._on_activate)

    def _on_activate(self, _app):
        win = WineInstallerWindow(application=self)
        win.present()


def main():
    app = RepackInstallerApp()
    return app.run(sys.argv)


if __name__ == '__main__':
    sys.exit(main())
