"""Backend Linux: KDE Plasma (KWin).

Janelas (listar, focar) passam por um script KWin carregado via D-Bus - no
Wayland nao existe API generica pra isso, cada compositor tem a sua. Apps e
icones vem do padrao freedesktop (.desktop + tema de icones), esses valem em
qualquer distro. Atalhos de teclado saem por ydotool, wtype ou xdotool - o
KWin nao injeta tecla, entao aqui depende de uma ferramenta externa.
"""
import configparser
import glob
import json
import os
import shutil
import subprocess
import tempfile
import threading

from gi.repository import Gio, GLib

APP_DIRS = [
    "/usr/share/applications",
    "/usr/local/share/applications",
    "/var/lib/flatpak/exports/share/applications",
    os.path.expanduser("~/.local/share/applications"),
    os.path.expanduser("~/.local/share/flatpak/exports/share/applications"),
]
ICON_DIRS = [
    os.path.expanduser("~/.local/share/icons"),
    os.path.expanduser("~/.icons"),
    "/usr/share/icons",
    "/usr/share/pixmaps",
    "/var/lib/flatpak/exports/share/icons",
    os.path.expanduser("~/.local/share/flatpak/exports/share/icons"),
]

BUS_NAME = "org.streamdeck.Deck"
SCRIPT_NAME = "streamdeck"
BUS_XML = ("<node><interface name='%s'>"
           "<method name='Result'><arg type='s' direction='in'/></method>"
           "</interface></node>" % BUS_NAME)

# O script KWin nao tem como devolver valor: `callDBus` e a unica saida. Entao o
# proprio deck vira um servico de sessao e recebe o JSON de volta por ai.
_bus = None
_reply = []
_replied = threading.Event()
# ponytail: uma trava global. E um celular mandando um toque por vez; se algum
# dia forem varios, use um nome de script por chamada em vez de serializar.
_kwin_lock = threading.Lock()

PRELUDE = ('function send(d){callDBus("%s","/","%s","Result",JSON.stringify(d));}\n'
           % (BUS_NAME, BUS_NAME))


def _serve_bus():
    global _bus
    if _bus is not None:
        return _bus
    _bus = Gio.bus_get_sync(Gio.BusType.SESSION)

    def on_call(_conn, _sender, _path, _iface, _method, params, invocation):
        _reply.append(params.unpack()[0])
        invocation.return_value(None)
        _replied.set()

    info = Gio.DBusNodeInfo.new_for_xml(BUS_XML).interfaces[0]
    _bus.register_object_with_closures2("/", info, on_call, None, None)
    Gio.bus_own_name_on_connection(_bus, BUS_NAME, Gio.BusNameOwnerFlags.NONE, None, None)
    threading.Thread(target=GLib.MainLoop().run, name="StreamDeck D-Bus", daemon=True).start()
    return _bus


def _kwin_call(path, iface, method, args=None):
    try:
        return _serve_bus().call_sync("org.kde.KWin", path, iface, method, args, None,
                                      Gio.DBusCallFlags.NONE, 5000, None)
    except GLib.Error as error:
        raise RuntimeError("KWin nao respondeu (este backend exige KDE Plasma 6): %s" % error)


def kwin(js):
    """Roda um script KWin e devolve o que ele mandar pro `send()`."""
    with _kwin_lock:
        path = os.path.join(tempfile.gettempdir(), "streamdeck-kwin.js")
        with open(path, "w", encoding="utf-8") as f:
            f.write(PRELUDE + js)
        _reply.clear()
        _replied.clear()
        unload = ("/Scripting", "org.kde.kwin.Scripting", "unloadScript",
                  GLib.Variant("(s)", (SCRIPT_NAME,)))
        _kwin_call(*unload)
        try:
            sid = _kwin_call("/Scripting", "org.kde.kwin.Scripting", "loadScript",
                             GLib.Variant("(ss)", (path, SCRIPT_NAME))).unpack()[0]
            _kwin_call("/Scripting/Script%d" % sid, "org.kde.kwin.Script", "run")
            if not _replied.wait(5):
                raise RuntimeError("o script do KWin nao respondeu em 5s")
            return json.loads(_reply[0])
        finally:
            _kwin_call(*unload)


# .desktop -------------------------------------------------------------------

_desktop_index = None


def desktop_files():
    """{id sem extensao: caminho do .desktop}. Diretorio do usuario ganha do sistema."""
    global _desktop_index
    if _desktop_index is None:
        _desktop_index = {}
        for d in APP_DIRS:
            for path in glob.glob(os.path.join(d, "*.desktop")):
                _desktop_index[os.path.basename(path)[:-8]] = path
    return _desktop_index


def entry(path):
    parser = configparser.RawConfigParser(interpolation=None, strict=False)
    try:
        parser.read(path, encoding="utf-8")
        return parser["Desktop Entry"]
    except (OSError, KeyError, configparser.Error):
        return {}


# icones ---------------------------------------------------------------------

_icon_index = None


def _read_theme():
    parser = configparser.RawConfigParser(interpolation=None, strict=False)
    try:
        parser.read(os.path.expanduser("~/.config/kdeglobals"), encoding="utf-8")
        return parser.get("Icons", "Theme", fallback="")
    except configparser.Error:
        return ""


def icon_index():
    """{nome do icone: melhor arquivo}. Uma varredura (~0.2s), depois so cache.

    Preferencia: tema atual > SVG (escala em qualquer tela) > maior PNG.
    """
    global _icon_index
    if _icon_index is not None:
        return _icon_index
    name = _read_theme()
    theme = ("/%s/" % name) if name else "\0"
    best = {}
    for root_dir in ICON_DIRS:
        for root, _dirs, files in os.walk(root_dir):
            size = next((int(p.split("x")[0]) for p in root.split(os.sep)
                         if p[:1].isdigit() and "x" in p and p.split("x")[0].isdigit()), 0)
            for f in files:
                name, _dot, ext = f.rpartition(".")
                if ext not in ("png", "svg"):
                    continue
                score = ((theme in root) * 100000 + (ext == "svg") * 10000
                         + min(size, 512) + ("/apps/" in root) * 1000)
                if score > best.get(name, (0,))[0]:
                    best[name] = (score, os.path.join(root, f))
    _icon_index = {name: path for name, (_score, path) in best.items()}
    return _icon_index


def icon_bytes(paths):
    """{caminho do .desktop: (extensao, bytes da imagem)}."""
    out = {}
    for path in paths:
        name = entry(path).get("Icon", "")
        if not name:
            continue
        found = name if os.path.isabs(name) else icon_index().get(name)
        if not found or not os.path.isfile(found):
            continue
        with open(found, "rb") as f:
            out[path] = (found.rsplit(".", 1)[1], f.read())
    return out


# teclado --------------------------------------------------------------------
# Nomes canonicos (deck.py) -> keysym do X11 (xdotool e wtype usam os mesmos) e
# codigo do evdev (ydotool fala direto com o kernel, em numero).
MOD_X11 = {"ctrl": "ctrl", "shift": "shift", "alt": "alt", "win": "super"}
MOD_WTYPE = {"ctrl": "ctrl", "shift": "shift", "alt": "alt", "win": "logo"}
MOD_EVDEV = {"ctrl": 29, "shift": 42, "alt": 56, "win": 125}

KEY_X11 = {
    "enter": "Return", "esc": "Escape", "tab": "Tab", "space": "space",
    "backspace": "BackSpace", "delete": "Delete", "insert": "Insert",
    "home": "Home", "end": "End", "pageup": "Page_Up", "pagedown": "Page_Down",
    "up": "Up", "down": "Down", "left": "Left", "right": "Right",
    "printscreen": "Print", "menu": "Menu", "capslock": "Caps_Lock",
    "volumeup": "XF86AudioRaiseVolume", "volumedown": "XF86AudioLowerVolume",
    "mute": "XF86AudioMute", "playpause": "XF86AudioPlay",
    "next": "XF86AudioNext", "prev": "XF86AudioPrev", "stop": "XF86AudioStop",
}
KEY_X11.update({c: c for c in "abcdefghijklmnopqrstuvwxyz0123456789"})
KEY_X11.update({"f%d" % n: "F%d" % n for n in range(1, 25)})
KEY_X11.update({name: name for name in ("comma period slash backslash semicolon "
                                        "apostrophe grave bracketleft bracketright "
                                        "minus equal").split()})

KEY_EVDEV = {
    "esc": 1, "minus": 12, "equal": 13, "backspace": 14, "tab": 15,
    "bracketleft": 26, "bracketright": 27, "enter": 28,
    "semicolon": 39, "apostrophe": 40, "grave": 41, "backslash": 43,
    "comma": 51, "period": 52, "slash": 53, "space": 57, "capslock": 58,
    "printscreen": 99, "home": 102, "up": 103, "pageup": 104, "left": 105,
    "right": 106, "end": 107, "down": 108, "pagedown": 109, "insert": 110,
    "delete": 111, "mute": 113, "volumedown": 114, "volumeup": 115,
    "menu": 127, "next": 163, "playpause": 164, "prev": 165, "stop": 166,
}
KEY_EVDEV.update(dict(zip("1234567890", range(2, 12))))
KEY_EVDEV.update(dict(zip("qwertyuiop", range(16, 26))))
KEY_EVDEV.update(dict(zip("asdfghjkl", range(30, 39))))
KEY_EVDEV.update(dict(zip("zxcvbnm", range(44, 51))))
KEY_EVDEV.update({"f%d" % n: 58 + n for n in range(1, 11)})       # F1..F10
KEY_EVDEV.update({"f11": 87, "f12": 88})
KEY_EVDEV.update({"f%d" % n: 170 + n for n in range(13, 25)})     # F13..F24

KEYS = frozenset(KEY_X11) & frozenset(KEY_EVDEV)
NO_TOOL = ("nenhuma ferramenta de teclado instalada - instale ydotool "
           "(sudo dnf install ydotool && systemctl --user enable --now ydotool), "
           "wtype ou xdotool")


def _xdotool(mods, key):
    return ["xdotool", "key", "--clearmodifiers",
            "+".join([MOD_X11[m] for m in mods] + [KEY_X11[key]])]


def _wtype(mods, key):
    cmd = ["wtype"]
    for mod in mods:
        cmd += ["-M", MOD_WTYPE[mod]]
    cmd += ["-k", KEY_X11[key]]
    for mod in mods:
        cmd += ["-m", MOD_WTYPE[mod]]
    return cmd


def _ydotool(mods, key):
    codes = [MOD_EVDEV[m] for m in mods] + [KEY_EVDEV[key]]
    # Aperta na ordem e solta ao contrario: o modificador sai depois da tecla.
    return (["ydotool", "key", "--key-delay=12"]
            + ["%d:1" % c for c in codes]
            + ["%d:0" % c for c in reversed(codes)])


def keyboard_tools():
    """Ferramentas na ordem de preferencia da sessao atual.

    No X11 o xdotool e o caminho curto. No Wayland ele so alcanca apps XWayland,
    entao vem por ultimo: o ydotool injeta pelo uinput (chega em tudo) e o wtype
    usa o teclado virtual do proprio compositor.
    """
    if os.environ.get("XDG_SESSION_TYPE") == "x11":
        return (("xdotool", _xdotool), ("ydotool", _ydotool), ("wtype", _wtype))
    return (("ydotool", _ydotool), ("wtype", _wtype), ("xdotool", _xdotool))


def send_keys(mods, key):
    for name, build in keyboard_tools():
        if not shutil.which(name):
            continue
        done = subprocess.run(build(mods, key), capture_output=True,
                              encoding="utf-8", errors="replace", timeout=10)
        if done.returncode == 0:
            return
        raise RuntimeError("%s falhou: %s" % (name, done.stderr.strip() or done.returncode))
    raise RuntimeError(NO_TOOL)


# contrato do deck.py --------------------------------------------------------

LIST_JS = """
var out = [], all = workspace.windowList();
for (var i = 0; i < all.length; i++) {
  var w = all[i];
  if (!w.normalWindow || w.skipTaskbar || !w.caption) continue;
  out.push({id: String(w.internalId), title: w.caption,
            app: w.desktopFileName || w.resourceClass || ""});
}
send(out);
"""

FOCUS_JS = """
var all = workspace.windowList(), hit = 0;
for (var i = 0; i < all.length; i++) {
  if (String(all[i].internalId) !== %s) continue;
  var w = all[i];
  hit = 1;
  w.minimized = false;
  if (w.desktops && w.desktops.length) workspace.currentDesktop = w.desktops[0];
  // Do celular nao da pra arrastar borda, entao maximiza sempre (igual no Windows).
  w.setMaximize(true, true);
  workspace.activeWindow = w;
}
send(hit);
"""


def list_windows():
    apps = desktop_files()
    out = []
    for w in kwin(LIST_JS):
        app = w["app"]
        path = apps.get(app) or apps.get(app.lower()) or ""
        out.append({"id": w["id"], "title": w["title"],
                    "app": entry(path).get("Name") or app,
                    "path": path})
    out.sort(key=lambda w: (w["app"].lower(), w["title"].lower()))
    return out


def focus(window_id):
    kwin(FOCUS_JS % json.dumps(str(window_id)))


def launch(path):
    """`.desktop` respeita Exec, Terminal e variaveis do ambiente; o resto e xdg-open."""
    if path.endswith(".desktop"):
        subprocess.Popen(["gio", "launch", path], start_new_session=True)
    else:
        subprocess.Popen(["xdg-open", path], start_new_session=True)
    # ponytail: sem esperar a janela aparecer - o KWin ja da foco a janela nova.
    # Se a prevencao de roubo de foco estiver alta, poderia focar em seguida.


def recents(favs):
    """Apps instalados. O Linux nao tem o equivalente do UserAssist do Windows."""
    seen = {a.get("path", "") for a in favs}
    out = []
    for path in sorted(set(desktop_files().values())):
        if path in seen:
            continue
        info = entry(path)
        if info.get("Type") != "Application" or not info.get("Name"):
            continue
        if info.get("NoDisplay", "").lower() == "true" or info.get("Hidden", "").lower() == "true":
            continue
        out.append({"name": info["Name"], "path": path})
    out.sort(key=lambda a: a["name"].lower())
    return out


def copy_text(text):
    subprocess.run(["wl-copy", "--", text], check=True, timeout=5)


def show_error(message):
    if subprocess.run(["kdialog", "--error", message], timeout=60).returncode:
        raise RuntimeError(message)


def selfcheck():
    windows = list_windows()
    assert windows, "nenhuma janela aberta - o script KWin nao listou nada"
    assert kwin(FOCUS_JS % json.dumps(windows[0]["id"])) == 1, "focar a janela falhou"
    assert icon_index(), "nenhum icone encontrado no tema"
    tool = next((n for n, _b in keyboard_tools() if shutil.which(n)), None)
    print("ok: KWin responde, %d icones indexados, tema %r, teclado por %s"
          % (len(icon_index()), _read_theme() or "(padrao)", tool or "(nada instalado)"))
    if not tool:
        print("aviso: atalhos de teclado indisponiveis - " + NO_TOOL)
