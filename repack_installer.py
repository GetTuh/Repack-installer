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
import urllib.parse
import urllib.request

APP_ID = 'io.github.GetTuh.RepackInstaller'
APP_VERSION = 'dev'
LOG_MAX_LINES = 2000
DATA_DIR = os.path.join(os.path.expanduser('~'), '.local', 'share', 'repackinstaller')
GAMES_ROOT = os.path.join(os.path.expanduser('~'), 'Games', 'Heroic', 'games-repacks')
SHARED_WINEPREFIX = os.path.join(GAMES_ROOT, 'shared-prefix')
GAMES_PREFIX_ROOT = GAMES_ROOT
BUNDLED_UMU_DIR = os.path.join(DATA_DIR, 'bin')
BUNDLED_UMU_PATH = os.path.join(BUNDLED_UMU_DIR, 'umu-run')
BUNDLED_UMU_VERSION_FILE = os.path.join(BUNDLED_UMU_DIR, '.version')
UMU_RELEASES_API = ('https://api.github.com/repos/'
                     'Open-Wine-Components/umu-launcher/releases/latest')

HEROIC_APP_ID = 'com.heroicgameslauncher.hgl'
HEROIC_CONFIG_CANDIDATES = [
    os.path.join(os.path.expanduser('~'), '.var', 'app', HEROIC_APP_ID, 'config', 'heroic'),
    os.path.join(os.path.expanduser('~'), '.config', 'heroic'),
]
HEROIC_DEFAULT_PREFIX_ROOT = os.path.join(os.path.expanduser('~'), 'Games', 'Heroic', 'Prefixes')

FAUGUS_APP_ID = 'io.github.Faugus.faugus-launcher'
# (config dir, data dir, is flatpak)
FAUGUS_CANDIDATES = [
    (os.path.join(os.path.expanduser('~'), '.config', 'faugus-launcher'),
     os.path.join(os.path.expanduser('~'), '.local', 'share', 'faugus-launcher'), False),
    (os.path.join(os.path.expanduser('~'), '.var', 'app', FAUGUS_APP_ID, 'config', 'faugus-launcher'),
     os.path.join(os.path.expanduser('~'), '.var', 'app', FAUGUS_APP_ID, 'data', 'faugus-launcher'), True),
]
FAUGUS_DEFAULT_PREFIX_ROOT = os.path.join(os.path.expanduser('~'), 'Faugus')
ART_SEARCH_URL = 'https://steamgrid.usebottles.com/api/search/'

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


def _fetch_art_url(title):
    url = ART_SEARCH_URL + urllib.parse.quote(title)
    req = urllib.request.Request(url, headers={'User-Agent': 'repack-installer'})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status != 200:
                return None
            data = json.loads(resp.read().decode())
            return data if isinstance(data, str) and data.startswith('http') else None
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            json.JSONDecodeError, OSError):
        return None


def _read_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def _heroic_prefix_root(heroic_dir):
    config = _read_json(os.path.join(heroic_dir, 'config.json'), {})
    defaults = config.get('defaultSettings', {}) if isinstance(config, dict) else {}
    prefix = defaults.get('winePrefix') if isinstance(defaults, dict) else None
    # Heroic puts per-game prefixes next to its default prefix, e.g. Prefixes/<Game Title>
    return os.path.dirname(os.path.expanduser(prefix)) if prefix else HEROIC_DEFAULT_PREFIX_ROOT


def _find_faugus():
    found = []
    for config_dir, data_dir, is_flatpak in FAUGUS_CANDIDATES:
        if os.path.isdir(config_dir) or os.path.isdir(data_dir):
            games = os.path.join(data_dir, 'games.json')
            mtime = os.path.getmtime(games) if os.path.exists(games) else 0
            found.append((mtime, config_dir, data_dir, is_flatpak))
    if not found:
        return None
    # Both native and flatpak installs may exist — prefer the one used most recently.
    _, config_dir, data_dir, is_flatpak = max(found, key=lambda f: f[0])
    return config_dir, data_dir, is_flatpak


def _faugus_prefix_root(config_dir):
    config = _read_json(os.path.join(config_dir, 'config.json'), {})
    prefix = config.get('default-prefix') if isinstance(config, dict) else None
    return os.path.expanduser(prefix) if prefix else FAUGUS_DEFAULT_PREFIX_ROOT


def _detect_launchers():
    launchers = {'heroic': None, 'faugus': None}
    heroic_dir = _find_heroic_config_dir()
    if heroic_dir:
        launchers['heroic'] = {
            'kind': 'heroic',
            'name': 'Heroic',
            'config_dir': heroic_dir,
            'flatpak': os.path.join('.var', 'app', HEROIC_APP_ID) in heroic_dir,
            'app_id': HEROIC_APP_ID,
            'proc_match': ['-x', 'heroic'],
            'command': 'heroic',
            'prefix_root': _heroic_prefix_root(heroic_dir),
        }
    faugus = _find_faugus()
    if faugus:
        config_dir, data_dir, is_flatpak = faugus
        launchers['faugus'] = {
            'kind': 'faugus',
            'name': 'Faugus',
            'config_dir': config_dir,
            'data_dir': data_dir,
            'flatpak': is_flatpak,
            'app_id': FAUGUS_APP_ID,
            # bracket trick stops pgrep/pkill matching the flatpak-spawn process itself
            'proc_match': ['-f', '[f]augus-launcher'],
            'command': 'faugus-launcher',
            'prefix_root': _faugus_prefix_root(config_dir),
        }
    return launchers


def _launcher_is_running(launcher):
    if launcher['flatpak']:
        result = subprocess.run(
            ['flatpak-spawn', '--host', 'flatpak', 'ps', '--columns=application'],
            capture_output=True, text=True
        )
        return launcher['app_id'] in result.stdout.split()
    result = subprocess.run(
        ['flatpak-spawn', '--host', 'pgrep'] + launcher['proc_match'],
        capture_output=True
    )
    return result.returncode == 0


def _restart_launcher(launcher):
    if launcher['flatpak']:
        subprocess.run(['flatpak-spawn', '--host', 'flatpak', 'kill', launcher['app_id']],
                       capture_output=True)
        cmd = ['flatpak-spawn', '--host', 'flatpak', 'run', launcher['app_id']]
    else:
        subprocess.run(['flatpak-spawn', '--host', 'pkill'] + launcher['proc_match'],
                       capture_output=True)
        cmd = ['flatpak-spawn', '--host', launcher['command']]
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _heroic_add_game(heroic_dir, title, executable, wineprefix, art_url=None):
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
        'art_cover': art_url or '',
        'is_installed': True,
        'art_square': art_url or '',
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


def _faugus_add_game(data_dir, title, executable, wineprefix):
    games_path = os.path.join(data_dir, 'games.json')
    games = []
    if os.path.exists(games_path):
        with open(games_path) as f:
            games = json.load(f)
        if not isinstance(games, list):
            raise ValueError('unexpected games.json format')

    taken = {g.get('gameid') for g in games if isinstance(g, dict)}
    base_id = _slugify(title)
    gameid, n = base_id, 2
    while gameid in taken:
        gameid, n = f'{base_id}-{n}', n + 1

    games.append({
        'gameid': gameid,
        'title': title,
        'path': executable,
        'prefix': wineprefix,
        'launch_arguments': '',
        'game_arguments': '',
        'mangohud': '',
        'gamemode': '',
        'sdl_enabled': '',
        'protonfix': '',
        'runner': 'Default',
        'addapp_enabled': '',
        'addapp': '',
        'addapp_bat': '',
        'addapp_delay': '',
        'addapp_first': False,
        'cover': '',
        'lossless_enabled': False,
        'lossless_multiplier': 1,
        'lossless_flow': 100,
        'lossless_performance': False,
        'lossless_hdr': False,
        'lossless_present': False,
        'playtime': 0,
        'hidden': False,
        'no_sleep': False,
        'category': False,
        'icon': '',
        'steamgriddb_id': '',
        'pre_launch': '',
        'post_launch': '',
        'steam_user': '',
        'disable_umu': '',
        'runtime': '',
        'last_played': '',
    })

    os.makedirs(data_dir, exist_ok=True)
    tmp_path = games_path + '.tmp'
    with open(tmp_path, 'w') as f:
        json.dump(games, f, indent=4, ensure_ascii=False)
    os.replace(tmp_path, games_path)
    return gameid


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
        self._pending_launcher = None
        self._launchers = _detect_launchers()
        self._prefix_custom = False
        self._setting_prefix = False
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

        self.launcher_group = Adw.PreferencesGroup(
            title='Launcher',
            description='Add the installed game to a launcher afterwards'
        )
        self.launcher_group.set_visible(False)
        main_box.append(self.launcher_group)

        self.launcher_checks = {}
        group_leader = None
        for kind, name in (('heroic', 'Heroic Games Launcher'), ('faugus', 'Faugus Launcher'),
                           ('none', 'None')):
            info = self._launchers.get(kind)
            if kind == 'none':
                subtitle = "Don't add to a launcher"
            elif info:
                subtitle = 'Detected' + (' (Flatpak)' if info['flatpak'] else '')
            else:
                subtitle = 'Not detected'
            row = Adw.ActionRow(title=name, subtitle=subtitle)
            check = Gtk.CheckButton()
            check.set_valign(Gtk.Align.CENTER)
            if group_leader:
                check.set_group(group_leader)
            else:
                group_leader = check
            check.connect('toggled', self._on_launcher_toggled)
            row.add_prefix(check)
            row.set_activatable_widget(check)
            row.set_sensitive(kind == 'none' or info is not None)
            self.launcher_group.add(row)
            self.launcher_checks[kind] = check

        default_kind = next((k for k in ('heroic', 'faugus') if self._launchers[k]), 'none')
        self.launcher_checks[default_kind].set_active(True)

        self.launcher_prefix_row = Adw.ActionRow(title="Install in launcher's prefix folder")
        self.launcher_prefix_row.set_subtitle_lines(0)
        self.launcher_prefix_switch = Gtk.Switch()
        self.launcher_prefix_switch.set_valign(Gtk.Align.CENTER)
        self.launcher_prefix_switch.connect('notify::active', self._on_prefix_mode_changed)
        self.launcher_prefix_row.add_suffix(self.launcher_prefix_switch)
        self.launcher_prefix_row.set_activatable_widget(self.launcher_prefix_switch)
        self.launcher_group.add(self.launcher_prefix_row)

        self.options_group = Adw.PreferencesGroup(title='Options')
        options_group = self.options_group
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
        self.per_game_switch.connect('notify::active', self._on_prefix_mode_changed)
        self.per_game_row.add_suffix(self.per_game_switch)
        self.per_game_row.set_activatable_widget(self.per_game_switch)
        options_group.add(self.per_game_row)

        self.prefix_entry_row = Adw.EntryRow(title='Wine prefix location')
        self.prefix_entry_row.set_sensitive(False)
        self.prefix_entry_row.connect('changed', self._on_prefix_edited)
        prefix_reset_btn = Gtk.Button(icon_name='edit-undo-symbolic')
        prefix_reset_btn.set_valign(Gtk.Align.CENTER)
        prefix_reset_btn.set_tooltip_text('Reset to the automatic location')
        prefix_reset_btn.connect('clicked', self._on_reset_prefix)
        self.prefix_entry_row.add_suffix(prefix_reset_btn)
        prefix_browse_btn = Gtk.Button(icon_name='folder-open-symbolic')
        prefix_browse_btn.set_valign(Gtk.Align.CENTER)
        prefix_browse_btn.set_tooltip_text('Choose a folder for the Wine prefix')
        prefix_browse_btn.connect('clicked', self._on_browse_prefix)
        self.prefix_entry_row.add_suffix(prefix_browse_btn)
        options_group.add(self.prefix_entry_row)
        self._update_launcher_prefix_row()

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

        manual_expander = Adw.ExpanderRow(
            title='Manual install',
            subtitle='Wine prefix path, for other launchers or manual setup'
        )
        self.prefix_group.add(manual_expander)

        self.prefix_row = Adw.ActionRow(
            title='Wine prefix',
            subtitle='Copy this into Heroic (or another launcher) as the Wine prefix for this game'
        )
        copy_btn = Gtk.Button(icon_name='edit-copy-symbolic')
        copy_btn.set_valign(Gtk.Align.CENTER)
        copy_btn.set_tooltip_text('Copy prefix path')
        copy_btn.connect('clicked', self._on_copy_prefix)
        self.prefix_row.add_suffix(copy_btn)
        manual_expander.add_row(self.prefix_row)

        prefix_hint = Gtk.Label(
            label=('When adding this game in Heroic, click "Show Wine settings" in the '
                  'Add Game dialog and paste this as the Wine Prefix.'),
            xalign=0
        )
        prefix_hint.set_wrap(True)
        prefix_hint.set_margin_start(12)
        prefix_hint.set_margin_end(12)
        prefix_hint.set_margin_top(6)
        prefix_hint.set_margin_bottom(6)
        prefix_hint.add_css_class('dim-label')
        prefix_hint.add_css_class('caption')
        manual_expander.add_row(prefix_hint)

        self.exe_row = Adw.EntryRow(title='Game Executable (best guess — verify this)')
        self.exe_row.set_visible(False)
        self.exe_row.connect('changed', self._on_exe_changed)
        exe_browse_btn = Gtk.Button(icon_name='document-open-symbolic')
        exe_browse_btn.set_valign(Gtk.Align.CENTER)
        exe_browse_btn.set_tooltip_text('Browse for the executable')
        exe_browse_btn.connect('clicked', self._on_browse_exe)
        self.exe_row.add_suffix(exe_browse_btn)
        exe_copy_btn = Gtk.Button(icon_name='edit-copy-symbolic')
        exe_copy_btn.set_valign(Gtk.Align.CENTER)
        exe_copy_btn.set_tooltip_text('Copy executable path')
        exe_copy_btn.connect('clicked', self._on_copy_exe)
        self.exe_row.add_suffix(exe_copy_btn)
        self.prefix_group.add(self.exe_row)

        self.launcher_row = Adw.ActionRow()
        self.launcher_row.set_subtitle_lines(0)
        self.launcher_row.set_visible(False)
        self.launcher_add_btn = Gtk.Button(label='Add')
        self.launcher_add_btn.add_css_class('suggested-action')
        self.launcher_add_btn.set_valign(Gtk.Align.CENTER)
        self.launcher_add_btn.connect('clicked', self._on_add_to_launcher)
        self.launcher_row.add_suffix(self.launcher_add_btn)
        self.prefix_group.add(self.launcher_row)

        self.restart_row = Adw.ActionRow(
            subtitle='Restart it to see the new game in your library'
        )
        self.restart_row.set_visible(False)
        self.restart_btn = Gtk.Button()
        self.restart_btn.set_valign(Gtk.Align.CENTER)
        self.restart_btn.connect('clicked', self._on_restart_launcher)
        self.restart_row.add_suffix(self.restart_btn)
        self.prefix_group.add(self.restart_row)

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

        version_label = Gtk.Label(label=f'v{APP_VERSION}')
        version_label.add_css_class('dim-label')
        version_label.add_css_class('caption')
        version_label.set_halign(Gtk.Align.CENTER)
        main_box.append(version_label)

    def _log(self, text):
        GLib.idle_add(self._append_log, text)

    def _append_log(self, text):
        end_iter = self.log_buffer.get_end_iter()
        self.log_buffer.insert(end_iter, text + '\n')

        overflow = self.log_buffer.get_line_count() - LOG_MAX_LINES
        if overflow > 0:
            _, cutoff = self.log_buffer.get_iter_at_line(overflow)
            self.log_buffer.delete(self.log_buffer.get_start_iter(), cutoff)

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
            self._prefix_custom = False
            self.prefix_entry_row.set_sensitive(True)
            self.launcher_group.set_visible(True)
            self._update_prefix_preview()

    def _on_name_changed(self, *_args):
        self._update_prefix_preview()

    def _on_prefix_mode_changed(self, *_args):
        # Switching the prefix mode is an explicit choice, so drop any hand-typed path.
        self._prefix_custom = False
        self._update_prefix_preview()

    def _selected_launcher(self):
        for kind, check in self.launcher_checks.items():
            if check.get_active():
                return self._launchers.get(kind)
        return None

    def _on_launcher_toggled(self, check):
        if not check.get_active() or not hasattr(self, 'prefix_entry_row'):
            return
        self._update_launcher_prefix_row()
        if self.launcher_prefix_switch.get_active():
            self._on_prefix_mode_changed()

    def _update_launcher_prefix_row(self):
        launcher = self._selected_launcher()
        if launcher:
            self.launcher_prefix_row.set_subtitle(launcher['prefix_root'])
            self.launcher_prefix_row.set_sensitive(True)
        else:
            self.launcher_prefix_row.set_subtitle('Select Heroic or Faugus to use its prefix folder')
            self.launcher_prefix_row.set_sensitive(False)
            self.launcher_prefix_switch.set_active(False)
        self.per_game_row.set_sensitive(not self._use_launcher_prefix())

    def _use_launcher_prefix(self):
        return self.launcher_prefix_switch.get_active() and self._selected_launcher() is not None

    def _effective_title(self):
        text = self.name_row.get_text().strip()
        return text or (_guess_title(self._exe_path) if self._exe_path else 'Installed Game')

    def _compute_wineprefix(self):
        slug = _slugify(self._effective_title())
        if self._use_launcher_prefix():
            return os.path.join(self._selected_launcher()['prefix_root'], slug)
        if self.per_game_switch.get_active():
            return os.path.join(GAMES_PREFIX_ROOT, slug)
        return SHARED_WINEPREFIX

    def _effective_wineprefix(self):
        text = os.path.expanduser(self.prefix_entry_row.get_text().strip())
        return os.path.abspath(text) if text else self._compute_wineprefix()

    def _update_prefix_preview(self):
        self.per_game_row.set_sensitive(not self._use_launcher_prefix())
        if not self._exe_path:
            self.options_group.set_description('Select a setup.exe first')
            return
        if not self._prefix_custom:
            self._setting_prefix = True
            self.prefix_entry_row.set_text(self._compute_wineprefix())
            self._setting_prefix = False
        self._update_prefix_note()

    def _update_prefix_note(self):
        path = self._effective_wineprefix()
        if os.path.isdir(os.path.join(path, 'drive_c')):
            note = 'Prefix already exists — will reuse'
        elif os.path.isdir(path) and os.listdir(path):
            note = 'Folder exists and is not empty — a prefix will be created inside it'
        else:
            note = 'Prefix will be created'
        self.options_group.set_description(note)

    def _on_prefix_edited(self, *_args):
        if self._setting_prefix:
            return
        self._prefix_custom = True
        self._update_prefix_note()

    def _on_reset_prefix(self, _btn):
        self._prefix_custom = False
        self._update_prefix_preview()

    def _on_browse_prefix(self, _btn):
        dialog = Gtk.FileDialog(title='Select Wine Prefix Folder')
        current = self._effective_wineprefix()
        for folder in (current, os.path.dirname(current)):
            if os.path.isdir(folder):
                dialog.set_initial_folder(Gio.File.new_for_path(folder))
                break
        dialog.select_folder(self, None, self._on_prefix_folder_chosen)

    def _on_prefix_folder_chosen(self, dialog, result):
        try:
            folder = dialog.select_folder_finish(result)
        except GLib.Error:
            return
        if folder:
            # Setting the text fires 'changed', which marks the prefix as custom.
            self.prefix_entry_row.set_text(folder.get_path())

    def _on_install(self, _btn):
        if not self._exe_path:
            return
        wineprefix = self._effective_wineprefix()
        title = self._effective_title()
        launcher = self._selected_launcher()
        self.install_btn.set_sensitive(False)
        self.browse_btn.set_sensitive(False)
        self.prefix_group.set_visible(False)
        self.launcher_row.set_visible(False)
        self.restart_row.set_visible(False)
        self.restart_btn.set_sensitive(True)
        self._set_status('Starting…', 'Launching umu-run…', True)
        threading.Thread(target=self._run_install, args=(wineprefix, title, launcher),
                         daemon=True).start()

    def _run_install(self, wineprefix, title, launcher):
        exe_dir = os.path.dirname(self._exe_path)
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

        self._idle(self._finish_install, wineprefix, title, candidates, launcher)
        self._idle(self.install_btn.set_sensitive, True)
        self._idle(self.browse_btn.set_sensitive, True)

    def _finish_install(self, wineprefix, title, candidates, launcher):
        self._last_prefix = wineprefix
        self.prefix_row.set_subtitle(wineprefix)
        self.prefix_group.set_visible(True)

        executable = candidates[0] if candidates else ''
        confident = len(candidates) == 1
        if candidates and not confident:
            self._log(f'Multiple candidate executables found, guessing the largest: {candidates}')
        elif not candidates:
            self._log('Could not automatically identify the installed game executable — '
                      'browse to it below')

        if candidates:
            self.exe_row.set_title('Game Executable' if confident else
                                   'Game Executable (multiple found — verify this)')
            self.exe_row.remove_css_class('warning')
        else:
            self.exe_row.set_title('Game Executable (not found — browse to select it)')
            self.exe_row.add_css_class('warning')
        self.exe_row.set_text(executable)
        self.exe_row.set_visible(True)

        if not launcher:
            self._pending_launcher = None
            self.launcher_row.set_visible(False)
            return GLib.SOURCE_REMOVE

        name = launcher['name']
        self._pending_launcher = {'launcher': launcher, 'title': title, 'wineprefix': wineprefix}
        self.launcher_row.set_title(f'Add to {name}')
        self.launcher_row.set_subtitle(f'Uses the game name and executable above for "{title}"')
        self.launcher_add_btn.set_label('Add')
        self.launcher_add_btn.set_sensitive(bool(executable))
        self.launcher_row.set_visible(True)
        self.restart_row.set_title(f'{name} is running')
        self.restart_row.set_subtitle('Restart it to see the new game in your library')
        self.restart_btn.set_label(f'Restart {name}')
        return GLib.SOURCE_REMOVE

    def _on_exe_changed(self, *_args):
        if self._pending_launcher is not None and self.launcher_add_btn.get_label() != 'Added':
            self.launcher_add_btn.set_sensitive(bool(self.exe_row.get_text().strip()))

    def _on_browse_exe(self, _btn):
        exe_filter = Gtk.FileFilter()
        exe_filter.set_name('Windows Executables (*.exe)')
        exe_filter.add_pattern('*.exe')
        exe_filter.add_pattern('*.EXE')

        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(exe_filter)

        dialog = Gtk.FileDialog(title='Select Game Executable')
        dialog.set_filters(filters)
        dialog.set_default_filter(exe_filter)

        if self._last_prefix:
            drive_c = os.path.join(self._last_prefix, 'drive_c')
            if os.path.isdir(drive_c):
                dialog.set_initial_folder(Gio.File.new_for_path(drive_c))

        dialog.open(self, None, self._on_exe_file_chosen)

    def _on_exe_file_chosen(self, dialog, result):
        try:
            file = dialog.open_finish(result)
        except GLib.Error:
            return
        if file:
            self.exe_row.set_text(file.get_path())
            self.exe_row.remove_css_class('warning')

    def _on_add_to_launcher(self, _btn):
        info = self._pending_launcher
        executable = self.exe_row.get_text().strip()
        if not info or not executable:
            return
        self.launcher_add_btn.set_sensitive(False)
        self.launcher_add_btn.set_label('Adding…')
        threading.Thread(target=self._add_to_launcher_flow, args=(info, executable),
                         daemon=True).start()

    def _add_to_launcher_flow(self, info, executable):
        launcher = info['launcher']
        name = launcher['name']
        title = info['title']
        try:
            if launcher['kind'] == 'heroic':
                self._log(f'Searching cover art for "{title}"…')
                art_url = _fetch_art_url(title)
                self._log(f'Found cover art: {art_url}' if art_url else 'No cover art found')
                game_id = _heroic_add_game(launcher['config_dir'], title, executable,
                                           info['wineprefix'], art_url=art_url)
            else:
                game_id = _faugus_add_game(launcher['data_dir'], title, executable,
                                           info['wineprefix'])
        except (OSError, ValueError) as e:
            self._log(f'Could not update {name} library: {e}')
            self._idle(self.launcher_row.set_subtitle, f'Failed: {e}')
            self._idle(self.launcher_add_btn.set_sensitive, True)
            self._idle(self.launcher_add_btn.set_label, 'Add')
            return

        self._idle(self.launcher_row.set_title, f'Added to {name}')
        self._idle(self.launcher_row.set_subtitle, f'"{title}" → {executable}')
        self._idle(self.launcher_add_btn.set_label, 'Added')
        self._log(f'Added to {name} library as "{title}" (id={game_id})')

        if _launcher_is_running(launcher):
            self._idle(self.restart_row.set_visible, True)

    def _on_restart_launcher(self, _btn):
        if not self._pending_launcher:
            return
        self.restart_btn.set_sensitive(False)
        self.restart_row.set_subtitle('Restarting…')
        threading.Thread(target=self._restart_launcher_flow,
                         args=(self._pending_launcher['launcher'],), daemon=True).start()

    def _restart_launcher_flow(self, launcher):
        _restart_launcher(launcher)
        self._idle(self.restart_row.set_subtitle, 'Restarted')
        self._log(f'Restarted {launcher["name"]}')

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
