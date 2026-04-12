#!/usr/bin/env python3
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, GLib, Gio

import os
import sys
import subprocess
import threading

APP_ID = 'io.github.RepackInstaller'
WINEPREFIX = os.path.join(
    os.environ.get('XDG_DATA_HOME', os.path.expanduser('~/.local/share')),
    'repackinstaller', 'prefix'
)


class WineInstallerWindow(Adw.ApplicationWindow):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.set_title('Repack Installer')
        self.set_default_size(500, 340)
        self._exe_path = None
        self._wine_proc = None
        self._build_ui()

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

        # --- File selection ---
        file_group = Adw.PreferencesGroup(title='Setup Executable')
        main_box.append(file_group)

        self.file_row = Adw.ActionRow(
            title='No file selected',
            subtitle='Choose a Windows installer (.exe)'
        )
        browse_btn = Gtk.Button(label='Browse…')
        browse_btn.add_css_class('suggested-action')
        browse_btn.set_valign(Gtk.Align.CENTER)
        browse_btn.connect('clicked', self._on_browse)
        self.file_row.add_suffix(browse_btn)
        file_group.add(self.file_row)

        # --- Status ---
        status_group = Adw.PreferencesGroup(title='Status')
        main_box.append(status_group)

        self.status_row = Adw.ActionRow(
            title='Idle',
            subtitle='Select a setup.exe to get started'
        )
        self.spinner = Gtk.Spinner()
        self.spinner.set_valign(Gtk.Align.CENTER)
        self.status_row.add_prefix(self.spinner)
        status_group.add(self.status_row)

        # --- Install button ---
        self.install_btn = Gtk.Button(label='Install')
        self.install_btn.add_css_class('suggested-action')
        self.install_btn.add_css_class('pill')
        self.install_btn.set_halign(Gtk.Align.CENTER)
        self.install_btn.set_sensitive(False)
        self.install_btn.connect('clicked', self._on_install)
        main_box.append(self.install_btn)

    # ------------------------------------------------------------------
    # File picker
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Install logic
    # ------------------------------------------------------------------

    def _on_install(self, _btn):
        if not self._exe_path:
            return
        self.install_btn.set_sensitive(False)
        self._set_status('Starting…', 'Launching Wine…', True)
        threading.Thread(target=self._run_install, daemon=True).start()

    def _run_install(self):
        env = os.environ.copy()
        env['WINEPREFIX'] = WINEPREFIX
        # Ensure Wine can reach the X display (required inside Flatpak sandbox)
        if 'DISPLAY' not in env:
            env['DISPLAY'] = ':0'

        # Create prefix directory so Wine doesn't complain.
        try:
            os.makedirs(WINEPREFIX, exist_ok=True)
        except OSError as e:
            self._idle(self._set_status, 'Error', f'Cannot create WINEPREFIX: {e}', False)
            self._idle(self.install_btn.set_sensitive, True)
            return

        # Use 'wine start /unix' — sets the working directory to the exe's
        # folder automatically, which repack installers require to find
        # their .bin parts next to setup.exe.
        exe_dir = os.path.dirname(self._exe_path)
        self._idle(self._set_status, 'Installing…', self._exe_path, True)
        try:
            self._wine_proc = subprocess.Popen(
                ['wine', 'start', '/unix', self._exe_path],
                env=env,
                cwd=exe_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            _stdout, stderr = self._wine_proc.communicate()
            rc = self._wine_proc.returncode
        except FileNotFoundError:
            self._idle(self._set_status, 'Error', 'wine binary not found.', False)
            self._idle(self.install_btn.set_sensitive, True)
            return
        except Exception as e:
            self._idle(self._set_status, 'Error', str(e)[:200], False)
            self._idle(self.install_btn.set_sensitive, True)
            return
        finally:
            self._wine_proc = None

        # Many NSIS/Inno Setup installers return non-zero even on success or
        # user-cancel, so we don't treat every non-zero as a hard failure.
        if rc == 0:
            self._idle(self._set_status, 'Done', 'Installation completed.', False)
        else:
            tail = stderr.decode(errors='replace').strip()[-200:]
            msg = tail or f'Installer exited with code {rc}.'
            self._idle(self._set_status, 'Finished', msg, False)

        self._idle(self.install_btn.set_sensitive, True)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _idle(fn, *args):
        """Schedule fn(*args) on the GTK main loop from any thread."""
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
