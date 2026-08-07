#!/usr/bin/env python3
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, GLib, Gio

import json
import os
import re
import secrets
import shutil
import string
import subprocess
import sys
import tarfile
import tempfile
import threading
import urllib.error
import urllib.request

APP_ID = 'io.github.GetTuh.RepackInstaller'
DATA_DIR = os.path.join(os.path.expanduser('~'), '.local', 'share', 'repackinstaller')
GAMES_ROOT = os.path.join(os.path.expanduser('~'), 'Games', 'Heroic', 'games-repacks')
SHARED_WINEPREFIX = os.path.join(GAMES_ROOT, 'shared-prefix')
GAMES_PREFIX_ROOT = GAMES_ROOT
BUNDLED_UMU_DIR = os.path.join(DATA_DIR, 'bin')
BUNDLED_UMU_PATH = os.path.join(BUNDLED_UMU_DIR, 'umu-run')
BUNDLED_UMU_VERSION_FILE = os.path.join(BUNDLED_UMU_DIR, '.version')
UMU_RELEASES_API = ('https://api.github.com/repos/'
                     'Open-Wine-Components/umu-launcher/releases/latest')

HEROIC_CONFIG_CANDIDATES = [
    os.path.join(os.path.expanduser('~'), '.var', 'app',
                 'com.heroicgameslauncher.hgl', 'config', 'heroic'),
    os.path.join(os.path.expanduser('~'), '.config', 'heroic'),
]

_EXE_DENYLIST_SUBSTRINGS = (
    'unins', 'vcredist', 'vc_redist', 'dxsetup', 'directx', 'dotnet',
    'oalinst', 'physxlegacy', 'redist', '_commonredist', 'vcruntime',
    'ueprereqsetup', 'crashreportclient', 'crashpad',
)

_REPACK_SUFFIX_RE = re.compile(
    r'[-._ ]*(FitGirl|DODI|CODEX|SKIDROW|PLAZA|RUNE|EMPRESS|TENOKE|Repack)s?$',
    re.IGNORECASE
)


def _umu_binary():
    if os.access(BUNDLED_UMU_PATH, os.X_OK):
        return BUNDLED_UMU_PATH
    return 'umu-run'


def _umu_cmd(*args, exe_dir=None, wineprefix=SHARED_WINEPREFIX):
    cmd = [
        'flatpak-spawn', '--host',
        f'--env=WINEPREFIX={wineprefix}',
        '--env=GAMEID=0',
        '--env=UMU_RUNTIME_UPDATE=0',
    ]
    if exe_dir:
        cmd += [f'--directory={exe_dir}']
    cmd += [_umu_binary()] + list(args)
    return cmd


def _slugify(text):
    text = re.sub(r'[^a-z0-9]+', '-', text.strip().lower()).strip('-')
    return text or 'game'


def _game_folder_name(exe_path):
    exe_dir = os.path.dirname(exe_path)
    parent_name = os.path.basename(exe_dir.rstrip(os.sep))
    return parent_name or os.path.splitext(os.path.basename(exe_path))[0]


def _guess_title(exe_path):
    base = _REPACK_SUFFIX_RE.sub('', _game_folder_name(exe_path))
    base = re.sub(r'[._]+', ' ', base).strip()
    return base or 'Installed Game'


def _new_app_name():
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(22))


def _snapshot_exes(drive_c):
    found = set()
    if not os.path.isdir(drive_c):
        return found
    for dirpath, dirnames, filenames in os.walk(drive_c):
        if dirpath == drive_c:
            dirnames[:] = [d for d in dirnames if d.lower() != 'windows']
        for name in filenames:
            if name.lower().endswith('.exe'):
                found.add(os.path.join(dirpath, name))
    return found


def _safe_size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _pick_new_exe(before, after):
    candidates = [
        p for p in (after - before)
        if not any(bad in os.path.basename(p).lower() for bad in _EXE_DENYLIST_SUBSTRINGS)
    ]
    candidates.sort(key=_safe_size, reverse=True)
    return candidates


def _find_heroic_config_dir():
    for path in HEROIC_CONFIG_CANDIDATES:
        if os.path.isdir(path):
            return path
    return None


def _heroic_add_game(heroic_dir, title, executable, wineprefix):
    library_path = os.path.join(heroic_dir, 'sideload_apps', 'library.json')
    library = {'games': []}
    if os.path.exists(library_path):
        with open(library_path) as f:
            library = json.load(f)
    library.setdefault('games', [])

    app_name = _new_app_name()
    library['games'].append({
        'runner': 'sideload',
        'app_name': app_name,
        'title': title,
        'install': {
            'executable': executable,
            'platform': 'Windows',
            'is_dlc': False
        },
        'folder_name': os.path.dirname(executable),
        'art_cover': '',
        'is_installed': True,
        'art_square': '',
        'canRunOffline': True,
        'browserUrl': '',
        'customUserAgent': '',
        'launchFullScreen': False
    })

    os.makedirs(os.path.dirname(library_path), exist_ok=True)
    with open(library_path, 'w') as f:
        json.dump(library, f, indent=2)

    games_config_dir = os.path.join(heroic_dir, 'GamesConfig')
    os.makedirs(games_config_dir, exist_ok=True)
    with open(os.path.join(games_config_dir, f'{app_name}.json'), 'w') as f:
        json.dump({app_name: {'winePrefix': wineprefix}, 'version': 'v0', 'explicit': True}, f, indent=2)

    return app_name


def _already_installed(output):
    return 'already installed' in output.lower()


def _download_file(url, dest_path):
    req = urllib.request.Request(url, headers={'User-Agent': 'repack-installer'})
    with urllib.request.urlopen(req, timeout=120) as resp, open(dest_path, 'wb') as out:
        shutil.copyfileobj(resp, out)


def _bundled_umu_version():
    try:
        with open(BUNDLED_UMU_VERSION_FILE) as f:
            return f.read().strip() or None
    except OSError:
        return None


def _version_tuple(v):
    return tuple(int(p) for p in re.findall(r'\d+', v)) or (0,)


def _fetch_latest_umu_zipapp():
    req = urllib.request.Request(
        UMU_RELEASES_API,
        headers={'Accept': 'application/vnd.github+json',
                 'User-Agent': 'repack-installer'}
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.load(resp)

    for asset in data.get('assets', []):
        if asset['name'].endswith('-zipapp.tar'):
            return asset['browser_download_url'], data.get('tag_name')
    return None, data.get('tag_name')


def _install_bundled_umu(url, tag):
    os.makedirs(BUNDLED_UMU_DIR, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        tar_path = os.path.join(tmp, 'umu.tar')
        _download_file(url, tar_path)
        with tarfile.open(tar_path) as tf:
            tf.extractall(tmp, filter='data')
        shutil.copy2(os.path.join(tmp, 'umu', 'umu-run'), BUNDLED_UMU_PATH)
    os.chmod(BUNDLED_UMU_PATH, 0o755)
    with open(BUNDLED_UMU_VERSION_FILE, 'w') as f:
        f.write(tag or '')


class WineInstallerWindow(Adw.ApplicationWindow):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.set_title('Repack Installer')
        self.set_default_size(500, 460)
        self._exe_path = None
        self._proc = None
        self._last_error = None
        self._last_prefix = None
        self._pending_heroic = None
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

        self.name_row = Adw.EntryRow(title='Game Name')
        self.name_row.connect('changed', self._on_name_changed)
        file_group.add(self.name_row)

        options_group = Adw.PreferencesGroup(title='Options')
        main_box.append(options_group)

        self.per_game_row = Adw.ActionRow(
            title='Separate Wine prefix per game (Recommended)',
            subtitle=('Installs into its own prefix instead of one shared prefix, so '
                      'redistributables like PhysX/vcredist stay isolated per game, and '
                      'you can point Heroic (or another launcher) at that exact prefix.')
        )
        self.per_game_row.set_subtitle_lines(0)
        self.per_game_switch = Gtk.Switch()
        self.per_game_switch.set_active(True)
        self.per_game_switch.set_valign(Gtk.Align.CENTER)
        self.per_game_switch.connect('notify::active', self._on_name_changed)
        self.per_game_row.add_suffix(self.per_game_switch)
        self.per_game_row.set_activatable_widget(self.per_game_switch)
        options_group.add(self.per_game_row)

        self.prefix_preview_row = Adw.ActionRow(title='Wine prefix location')
        self.prefix_preview_row.set_subtitle('Select a setup.exe first')
        self.prefix_preview_row.set_subtitle_lines(0)
        options_group.add(self.prefix_preview_row)

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

        self.prefix_group = Adw.PreferencesGroup(title='Result')
        self.prefix_group.set_visible(False)
        main_box.append(self.prefix_group)

        self.prefix_row = Adw.ActionRow(
            title='Wine prefix',
            subtitle='Copy this into Heroic (or another launcher) as the Wine prefix for this game'
        )
        copy_btn = Gtk.Button(icon_name='edit-copy-symbolic')
        copy_btn.set_valign(Gtk.Align.CENTER)
        copy_btn.set_tooltip_text('Copy prefix path')
        copy_btn.connect('clicked', self._on_copy_prefix)
        self.prefix_row.add_suffix(copy_btn)
        self.prefix_group.add(self.prefix_row)

        prefix_hint = Gtk.Label(
            label=('When adding this game in Heroic, click "Show Wine settings" in the '
                  'Add Game dialog and paste this as the Wine Prefix.'),
            xalign=0
        )
        prefix_hint.set_wrap(True)
        prefix_hint.set_margin_start(12)
        prefix_hint.set_margin_end(12)
        prefix_hint.set_margin_bottom(6)
        prefix_hint.add_css_class('dim-label')
        prefix_hint.add_css_class('caption')
        self.prefix_group.add(prefix_hint)

        self.exe_row = Adw.EntryRow(title='Game Executable (best guess — verify this)')
        self.exe_row.set_visible(False)
        exe_copy_btn = Gtk.Button(icon_name='edit-copy-symbolic')
        exe_copy_btn.set_valign(Gtk.Align.CENTER)
        exe_copy_btn.set_tooltip_text('Copy executable path')
        exe_copy_btn.connect('clicked', self._on_copy_exe)
        self.exe_row.add_suffix(exe_copy_btn)
        self.prefix_group.add(self.exe_row)

        self.heroic_row = Adw.ActionRow()
        self.heroic_row.set_subtitle_lines(0)
        self.heroic_row.set_visible(False)
        self.heroic_add_btn = Gtk.Button(label='Add')
        self.heroic_add_btn.add_css_class('suggested-action')
        self.heroic_add_btn.set_valign(Gtk.Align.CENTER)
        self.heroic_add_btn.connect('clicked', self._on_add_to_heroic)
        self.heroic_row.add_suffix(self.heroic_add_btn)
        self.prefix_group.add(self.heroic_row)

        log_label = Gtk.Label(label='Log', xalign=0)
        log_label.add_css_class('heading')
        main_box.append(log_label)

        log_scroller = Gtk.ScrolledWindow()
        log_scroller.set_min_content_height(140)
        log_scroller.set_vexpand(True)
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
        main_box.append(log_scroller)

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

        if os.access(BUNDLED_UMU_PATH, os.X_OK):
            self._idle(self._set_status, 'Ready', 'Select a setup.exe to get started', False)
            self._idle(self.browse_btn.set_sensitive, True)
            self._check_for_umu_update()
            return

        self._idle(self._prompt_install_umu)

    def _prompt_install_umu(self):
        dialog = Adw.AlertDialog(
            heading='Install umu-run?',
            body='umu-run was not found. Download a portable copy from the latest '
                 'umu-launcher release on GitHub? No system changes or root access needed.'
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
            self._set_status('Preparing…', 'Getting ready to install umu-run', True)
            threading.Thread(target=self._install_umu_flow, daemon=True).start()
        else:
            self._set_status('umu-run not found',
                             'Install umu-launcher on the host system (pre-installed on Bazzite)', False)

    def _install_umu_flow(self):
        self._idle(self._set_status, 'Checking latest umu-launcher release…',
                   'Contacting GitHub', True)
        try:
            url, tag = _fetch_latest_umu_zipapp()
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            self._idle(self._set_status, 'Error', f'Failed to check releases: {e}'[:200], False)
            return
        if not url:
            self._idle(self._set_status, 'Error',
                       'No zipapp asset found in the latest umu-launcher release', False)
            return

        self._idle(self._set_status, f'Downloading umu-run {tag}…', url, True)
        try:
            _install_bundled_umu(url, tag)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
                OSError, tarfile.TarError) as e:
            self._idle(self._set_status, 'Error', f'Install failed: {e}'[:200], False)
            return

        self._idle(self._set_status, 'Ready',
                   f'umu-run {tag} installed. Select a setup.exe to get started', False)
        self._idle(self.browse_btn.set_sensitive, True)
        self._log(f'Installed bundled umu-run {tag} at {BUNDLED_UMU_PATH}')

    def _check_for_umu_update(self):
        current = _bundled_umu_version()
        try:
            url, tag = _fetch_latest_umu_zipapp()
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
            return
        if not url or not tag:
            return
        if current and _version_tuple(tag) <= _version_tuple(current):
            return
        self._idle(self._prompt_umu_update, url, tag, current)

    def _prompt_umu_update(self, url, tag, current):
        dialog = Adw.AlertDialog(
            heading='umu-run update available',
            body=f'{current or "unknown version"} → {tag}. Update the bundled copy now?'
        )
        dialog.add_response('skip', 'Skip')
        dialog.add_response('update', 'Update')
        dialog.set_response_appearance('update', Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response('update')
        dialog.set_close_response('skip')
        dialog.connect('response', self._on_umu_update_response, url, tag)
        dialog.present(self)
        return GLib.SOURCE_REMOVE

    def _on_umu_update_response(self, _dialog, response, url, tag):
        if response == 'update':
            threading.Thread(target=self._update_umu_flow, args=(url, tag), daemon=True).start()

    def _update_umu_flow(self, url, tag):
        self._idle(self._set_status, f'Updating umu-run to {tag}…', url, True)
        try:
            _install_bundled_umu(url, tag)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
                OSError, tarfile.TarError) as e:
            self._idle(self._set_status, 'Error', f'Update failed: {e}'[:200], False)
            return
        self._idle(self._set_status, 'Ready', f'umu-run updated to {tag}', False)
        self._log(f'Updated bundled umu-run to {tag}')

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
            self.name_row.set_text(_guess_title(self._exe_path))
            self.install_btn.set_sensitive(True)
            self._set_status('Ready', f'Selected: {name}', False)
            self._update_prefix_preview()

    def _on_name_changed(self, *_args):
        self._update_prefix_preview()

    def _effective_title(self):
        text = self.name_row.get_text().strip()
        return text or (_guess_title(self._exe_path) if self._exe_path else 'Installed Game')

    def _compute_wineprefix(self):
        if self.per_game_switch.get_active():
            return os.path.join(GAMES_PREFIX_ROOT, _slugify(self._effective_title()))
        return SHARED_WINEPREFIX

    def _update_prefix_preview(self):
        if not self._exe_path:
            self.prefix_preview_row.set_subtitle('Select a setup.exe first')
            return
        path = self._compute_wineprefix()
        note = ' (already exists — will reuse)' if os.path.isdir(path) else ' (will be created)'
        self.prefix_preview_row.set_subtitle(path + note)

    def _on_install(self, _btn):
        if not self._exe_path:
            return
        self.install_btn.set_sensitive(False)
        self.browse_btn.set_sensitive(False)
        self.prefix_group.set_visible(False)
        self.heroic_row.set_visible(False)
        self._set_status('Starting…', 'Launching umu-run…', True)
        threading.Thread(target=self._run_install, daemon=True).start()

    def _run_install(self):
        exe_dir = os.path.dirname(self._exe_path)
        wineprefix = self._compute_wineprefix()
        title = self._effective_title()
        self._log(f'Using Wine prefix: {wineprefix}')

        self._idle(self._set_status, 'Installing vcrun2022…', 'winetricks vcrun2022', True)
        if not self._run_cmd(_umu_cmd('winetricks', 'vcrun2022', wineprefix=wineprefix),
                             label='winetricks', success_check=_already_installed):
            self._idle(self._set_status, 'Error', self._last_error or 'winetricks failed', False)
            self._idle(self.install_btn.set_sensitive, True)
            self._idle(self.browse_btn.set_sensitive, True)
            return

        drive_c = os.path.join(wineprefix, 'drive_c')
        before_exes = _snapshot_exes(drive_c)

        self._idle(self._set_status, 'Running installer…', self._exe_path, True)
        installer_ok = self._run_cmd(
            _umu_cmd(self._exe_path, exe_dir=exe_dir, wineprefix=wineprefix), label='umu-run')
        if not installer_ok:
            # umu-run's exit code isn't a reliable success signal here (unlike winetricks,
            # there's no known-good text pattern to check) — Windows installers under Wine
            # often exit non-zero even after installing fine. Fall back to checking whether
            # anything actually got installed before treating this as fatal.
            self._log(f'Installer exited non-zero ({self._last_error}); '
                      'checking whether files were installed anyway')

        candidates = _pick_new_exe(before_exes, _snapshot_exes(drive_c))

        if not installer_ok and not candidates:
            self._idle(self._set_status, 'Error', self._last_error or 'Installer failed', False)
            self._idle(self.install_btn.set_sensitive, True)
            self._idle(self.browse_btn.set_sensitive, True)
            return

        if not installer_ok:
            self._idle(self._set_status, 'Done (installer exited non-zero)',
                       'Files were installed anyway — worth double-checking the game runs', False)
        else:
            self._idle(self._set_status, 'Done', 'Installer finished.', False)

        self._idle(self._finish_install, wineprefix, title, candidates)
        self._idle(self.install_btn.set_sensitive, True)
        self._idle(self.browse_btn.set_sensitive, True)

    def _finish_install(self, wineprefix, title, candidates):
        self._last_prefix = wineprefix
        self.prefix_row.set_subtitle(wineprefix)
        self.prefix_group.set_visible(True)

        if not candidates:
            self.exe_row.set_visible(False)
            self.heroic_row.set_visible(False)
            self._log('Could not identify the installed game executable — '
                      'add it to Heroic manually using the prefix above')
            return GLib.SOURCE_REMOVE

        executable = candidates[0]
        confident = len(candidates) == 1
        if not confident:
            self._log(f'Multiple candidate executables found, guessing the largest: {candidates}')

        self.exe_row.set_title('Game Executable' if confident else
                               'Game Executable (multiple found — verify this)')
        self.exe_row.set_text(executable)
        self.exe_row.set_visible(True)

        heroic_dir = _find_heroic_config_dir()
        if not heroic_dir:
            self.heroic_row.set_visible(False)
            self._log('Heroic not detected on this system')
            return GLib.SOURCE_REMOVE

        self._pending_heroic = {'heroic_dir': heroic_dir, 'title': title, 'wineprefix': wineprefix}
        self.heroic_row.set_title('Add to Heroic')
        self.heroic_row.set_subtitle(f'Uses the game name and executable above for "{title}"')
        self.heroic_add_btn.set_label('Add')
        self.heroic_add_btn.set_sensitive(True)
        self.heroic_row.set_visible(True)
        return GLib.SOURCE_REMOVE

    def _on_add_to_heroic(self, _btn):
        info = self._pending_heroic
        executable = self.exe_row.get_text().strip()
        if not info or not executable:
            return
        try:
            app_name = _heroic_add_game(
                info['heroic_dir'], info['title'], executable, info['wineprefix'])
        except (OSError, json.JSONDecodeError) as e:
            self._log(f'Could not update Heroic library: {e}')
            self.heroic_row.set_subtitle(f'Failed: {e}')
            return
        self.heroic_row.set_title('Added to Heroic')
        self.heroic_row.set_subtitle(f'"{info["title"]}" → {executable}')
        self.heroic_add_btn.set_sensitive(False)
        self.heroic_add_btn.set_label('Added')
        self._log(f'Added to Heroic library as "{info["title"]}" (app_name={app_name})')

    def _on_copy_prefix(self, _btn):
        if self._last_prefix:
            self.get_clipboard().set(self._last_prefix)

    def _on_copy_exe(self, _btn):
        text = self.exe_row.get_text().strip()
        if text:
            self.get_clipboard().set(text)

    def _run_cmd(self, cmd, label, success_check=None):
        print(f'[repack-installer] {label}: {cmd}', flush=True)
        self._log(f'$ {label}: {" ".join(cmd)}')
        self._last_error = None
        output_lines = []
        try:
            self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                          stderr=subprocess.STDOUT)
            for line in self._proc.stdout:
                text = line.decode(errors='replace').rstrip()
                output_lines.append(text)
                print(f'[{label}] {text}', flush=True)
                self._log(f'[{label}] {text}')
            rc = self._proc.wait()
            print(f'[repack-installer] {label} exited rc={rc}', flush=True)
            self._log(f'[{label}] exited rc={rc}')
            ok = rc == 0
            if not ok and success_check and success_check('\n'.join(output_lines)):
                self._log(f'[{label}] treating exit code {rc} as success')
                ok = True
            if not ok:
                self._last_error = f'{label} exited with code {rc}'
            return ok
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
