#!/usr/bin/env python3
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, GLib, Gio

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.request

APP_ID = 'io.github.GetTuh.RepackInstaller'
WINEPREFIX = os.path.join(os.path.expanduser('~'), '.local', 'share',
                          'repackinstaller', 'prefix')
UMU_RELEASES_API = ('https://api.github.com/repos/'
                     'Open-Wine-Components/umu-launcher/releases/latest')


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


def _host_has_cmd(cmd):
    result = subprocess.run(
        ['flatpak-spawn', '--host', 'sh', '-c', f'command -v {cmd}'],
        capture_output=True
    )
    return result.returncode == 0


def _get_os_release():
    result = subprocess.run(
        ['flatpak-spawn', '--host', 'cat', '/etc/os-release'],
        capture_output=True, text=True
    )
    info = {}
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            if '=' in line:
                key, _, value = line.partition('=')
                info[key] = value.strip().strip('"')
    return info


def _is_fedora(os_release):
    id_like = os_release.get('ID_LIKE', '').lower()
    return os_release.get('ID', '').lower() == 'fedora' or 'fedora' in id_like


def _fetch_latest_umu_rpm(fedora_version):
    req = urllib.request.Request(
        UMU_RELEASES_API,
        headers={'Accept': 'application/vnd.github+json',
                 'User-Agent': 'repack-installer'}
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.load(resp)

    assets = data.get('assets', [])
    rpms = [a for a in assets
            if a['name'].startswith('umu-launcher-')
            and a['name'].endswith('.x86_64.rpm')]
    if not rpms:
        return None, data.get('tag_name')

    def fc_num(asset):
        m = re.search(r'\.fc(\d+)\.', asset['name'])
        return int(m.group(1)) if m else -1

    if fedora_version:
        for asset in rpms:
            if str(fc_num(asset)) == str(fedora_version):
                return asset['browser_download_url'], data.get('tag_name')

    # No exact match for this Fedora version: fall back to the newest build.
    rpms.sort(key=fc_num, reverse=True)
    return rpms[0]['browser_download_url'], data.get('tag_name')


def _download_file(url, dest_path):
    req = urllib.request.Request(url, headers={'User-Agent': 'repack-installer'})
    with urllib.request.urlopen(req, timeout=120) as resp, open(dest_path, 'wb') as out:
        shutil.copyfileobj(resp, out)


class WineInstallerWindow(Adw.ApplicationWindow):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.set_title('Repack Installer')
        self.set_default_size(500, 340)
        self._exe_path = None
        self._proc = None
        self._last_error = None
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

        log_expander = Gtk.Expander(label='Log')
        main_box.append(log_expander)

        log_scroller = Gtk.ScrolledWindow()
        log_scroller.set_min_content_height(140)
        log_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        log_scroller.add_css_class('card')

        self.log_buffer = Gtk.TextBuffer()
        self.log_view = Gtk.TextView(buffer=self.log_buffer)
        self.log_view.set_editable(False)
        self.log_view.set_cursor_visible(False)
        self.log_view.set_monospace(True)
        self.log_view.set_margin_top(6)
        self.log_view.set_margin_bottom(6)
        self.log_view.set_margin_start(6)
        self.log_view.set_margin_end(6)
        log_scroller.set_child(self.log_view)

        log_expander.set_child(log_scroller)

    def _log(self, text):
        GLib.idle_add(self._append_log, text)

    def _append_log(self, text):
        end_iter = self.log_buffer.get_end_iter()
        self.log_buffer.insert(end_iter, text + '\n')
        self.log_view.scroll_mark_onscreen(self.log_buffer.get_insert())
        return GLib.SOURCE_REMOVE

    def _check_umu(self):
        result = subprocess.run(
            ['flatpak-spawn', '--host', 'sh', '-c', 'command -v umu-run'],
            capture_output=True
        )
        if result.returncode == 0:
            self._idle(self._set_status, 'Ready', 'Select a setup.exe to get started', False)
            self._idle(self.browse_btn.set_sensitive, True)
            return

        os_release = _get_os_release()
        if _is_fedora(os_release):
            self._idle(self._set_status, 'umu-run not found',
                       'Fedora detected', False)
            self._idle(self._prompt_install_umu)
        else:
            self._idle(self._set_status, 'umu-run not found',
                       'Install umu-launcher on the host system (pre-installed on Bazzite)', False)

    def _prompt_install_umu(self):
        dialog = Adw.AlertDialog(
            heading='Install umu-launcher?',
            body='umu-run was not found. Download and install the latest '
                 'umu-launcher release from GitHub on this Fedora system?'
        )
        dialog.add_response('cancel', 'Cancel')
        dialog.add_response('install', 'Install')
        dialog.set_response_appearance('install', Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response('install')
        dialog.set_close_response('cancel')
        dialog.connect('response', self._on_install_umu_response)
        dialog.present(self)
        return GLib.SOURCE_REMOVE

    def _on_install_umu_response(self, _dialog, response):
        if response == 'install':
            self._set_status('Preparing…', 'Getting ready to install umu-launcher', True)
            threading.Thread(target=self._install_umu_flow, daemon=True).start()
        else:
            self._set_status('umu-run not found',
                             'Install umu-launcher on the host system (pre-installed on Bazzite)', False)

    def _install_umu_flow(self):
        os_release = _get_os_release()
        fedora_version = os_release.get('VERSION_ID')

        self._idle(self._set_status, 'Checking latest umu-launcher release…',
                   'Contacting GitHub', True)
        try:
            rpm_url, tag = _fetch_latest_umu_rpm(fedora_version)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            self._idle(self._set_status, 'Error', f'Failed to check releases: {e}'[:200], False)
            return
        if not rpm_url:
            self._idle(self._set_status, 'Error',
                       'No Fedora rpm found in the latest umu-launcher release', False)
            return

        dest_dir = os.path.join(os.path.expanduser('~'), '.cache', 'repackinstaller')
        os.makedirs(dest_dir, exist_ok=True)
        dest_path = os.path.join(dest_dir, os.path.basename(rpm_url))

        self._idle(self._set_status, f'Downloading umu-launcher {tag}…', rpm_url, True)
        try:
            _download_file(rpm_url, dest_path)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
            self._idle(self._set_status, 'Error', f'Download failed: {e}'[:200], False)
            return

        if _host_has_cmd('dnf'):
            install_cmd = ['flatpak-spawn', '--host', 'pkexec', 'dnf', 'install', '-y', dest_path]
            note = 'Requesting authentication (pkexec dnf install)'
        elif _host_has_cmd('rpm-ostree'):
            install_cmd = ['flatpak-spawn', '--host', 'pkexec', 'rpm-ostree', 'install', '-y', dest_path]
            note = 'Requesting authentication (pkexec rpm-ostree install); a reboot will be needed'
        else:
            self._idle(self._set_status, 'Error',
                       'Neither dnf nor rpm-ostree found on the host', False)
            return

        self._idle(self._set_status, 'Installing umu-launcher…', note, True)
        if not self._run_cmd(install_cmd, label='install-umu'):
            self._idle(self._set_status, 'Error',
                       self._last_error or 'umu-launcher installation failed or was cancelled', False)
            return

        result = subprocess.run(
            ['flatpak-spawn', '--host', 'sh', '-c', 'command -v umu-run'],
            capture_output=True
        )
        if result.returncode == 0:
            self._idle(self._set_status, 'Ready',
                       'umu-launcher installed. Select a setup.exe to get started', False)
            self._idle(self.browse_btn.set_sensitive, True)
        else:
            self._idle(self._set_status, 'Error',
                       'umu-run still not found after install (a reboot may be required)', False)

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
            self._idle(self._set_status, 'Error', self._last_error or 'winetricks failed', False)
            self._idle(self.install_btn.set_sensitive, True)
            self._idle(self.browse_btn.set_sensitive, True)
            return

        self._idle(self._set_status, 'Running installer…', self._exe_path, True)
        if not self._run_cmd(_umu_cmd(self._exe_path, exe_dir=exe_dir), label='umu-run'):
            self._idle(self._set_status, 'Error', self._last_error or 'Installer failed', False)
            self._idle(self.install_btn.set_sensitive, True)
            self._idle(self.browse_btn.set_sensitive, True)
            return

        self._idle(self._set_status, 'Done', 'Installer finished.', False)
        self._idle(self.install_btn.set_sensitive, True)
        self._idle(self.browse_btn.set_sensitive, True)

    def _run_cmd(self, cmd, label):
        print(f'[repack-installer] {label}: {cmd}', flush=True)
        self._log(f'$ {label}: {" ".join(cmd)}')
        self._last_error = None
        try:
            self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                          stderr=subprocess.STDOUT)
            for line in self._proc.stdout:
                text = line.decode(errors='replace').rstrip()
                print(f'[{label}] {text}', flush=True)
                self._log(f'[{label}] {text}')
            rc = self._proc.wait()
            print(f'[repack-installer] {label} exited rc={rc}', flush=True)
            self._log(f'[{label}] exited rc={rc}')
            if rc != 0:
                self._last_error = f'{label} exited with code {rc}'
            return rc == 0
        except Exception as e:
            self._last_error = str(e)[:200]
            self._log(f'[{label}] error: {self._last_error}')
            return False
        finally:
            self._proc = None

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
        self._log(f'{title} — {subtitle}' if subtitle else title)
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
