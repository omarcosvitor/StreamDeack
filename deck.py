"""StreamDeck de bolso.

Uso:
    python deck.py           inicia na bandeja do Windows
    python deck.py serve     inicia no terminal e imprime a URL
    python deck.py check     autoteste

No celular: abre a URL no Chrome, menu -> Compartilhar -> "Adicionar a tela
inicial". O atalho abre em tela cheia, deitado.

Gestos: desliza pro lado troca a pagina de favoritos, pra cima mostra os apps
abertos, pra baixo os usados recentemente. Toque abre, toque longo adiciona ou
remove dos favoritos.

Favoritos ficam em apps.json (name + path). path aceita .exe, .lnk, pasta ou URL.
Porta: variavel de ambiente DECK_PORT (padrao 8765).
"""
import base64
import codecs
import gzip
import hashlib
import http.server
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.request
import webbrowser
import zlib

# O PyInstaller extrai os arquivos empacotados em ``sys._MEIPASS``. A
# configuracao, por outro lado, precisa ficar fora da pasta do programa para
# continuar gravavel depois da instalacao e sobreviver a atualizacoes.
BUNDLE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
if getattr(sys, "frozen", False):
    DATA_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "StreamDeck")
else:
    DATA_DIR = BUNDLE_DIR
FAVS = os.path.join(DATA_DIR, "apps.json")
PORT = int(os.environ.get("DECK_PORT", 8765))
RECENT_MAX = int(os.environ.get("DECK_RECENT_MAX", 24))
# ponytail: com a sessao bloqueada esse processo segura o foreground e ninguem
# toma o lugar dele - a trava so da pra testar com a area de trabalho aberta.
LOCK_SCREEN = "LockApp"
USER_ASSIST = r"Software\Microsoft\Windows\CurrentVersion\Explorer\UserAssist"

# ponytail: janelas, foco, lancamento e recentes falam direto com o Win32 e com
# o registro. A versao antiga abria um powershell.exe por acao - e o Add-Type
# ainda chamava o compilador de C# a cada toque, o que custava segundos.
_WINDOWS = sys.platform == "win32"

if _WINDOWS:
    import ctypes
    import winreg
    from ctypes import wintypes

    GW_OWNER = 4
    SW_MAXIMIZE = 3
    VK_MENU = 0x12
    VK_SHIFT = 0x10
    KEYEVENTF_KEYUP = 2
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _ENUM_PROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _bind(dll, name, restype, *argtypes):
        fn = getattr(dll, name)
        fn.restype = restype
        fn.argtypes = argtypes
        return fn

    _EnumWindows = _bind(_user32, "EnumWindows", wintypes.BOOL, _ENUM_PROC, wintypes.LPARAM)
    _IsWindowVisible = _bind(_user32, "IsWindowVisible", wintypes.BOOL, wintypes.HWND)
    _GetWindow = _bind(_user32, "GetWindow", wintypes.HWND, wintypes.HWND, wintypes.UINT)
    _GetWindowTextLengthW = _bind(_user32, "GetWindowTextLengthW", ctypes.c_int, wintypes.HWND)
    _GetWindowTextW = _bind(_user32, "GetWindowTextW", ctypes.c_int,
                            wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    _GetWindowThreadProcessId = _bind(_user32, "GetWindowThreadProcessId", wintypes.DWORD,
                                      wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    _GetForegroundWindow = _bind(_user32, "GetForegroundWindow", wintypes.HWND)
    _SetForegroundWindow = _bind(_user32, "SetForegroundWindow", wintypes.BOOL, wintypes.HWND)
    _ShowWindow = _bind(_user32, "ShowWindow", wintypes.BOOL, wintypes.HWND, ctypes.c_int)
    _keybd_event = _bind(_user32, "keybd_event", None, ctypes.c_ubyte, ctypes.c_ubyte,
                         wintypes.DWORD, ctypes.c_size_t)
    _OpenProcess = _bind(_kernel32, "OpenProcess", wintypes.HANDLE,
                         wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _CloseHandle = _bind(_kernel32, "CloseHandle", wintypes.BOOL, wintypes.HANDLE)
    _QueryFullProcessImageNameW = _bind(_kernel32, "QueryFullProcessImageNameW", wintypes.BOOL,
                                        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                        ctypes.POINTER(wintypes.DWORD))

# ponytail: unico script que sobrou. Extrair icone precisa de GDI+, e o
# resultado fica em cache - roda uma vez por executavel, nao por requisicao.
PS_ICONS = r"""
Add-Type -AssemblyName System.Drawing
$code = @"
using System;
using System.Drawing;
using System.Drawing.Imaging;
using System.IO;
using System.Runtime.InteropServices;

public static class DeckIcon {
  [DllImport("user32.dll", CharSet = CharSet.Unicode)]
  static extern uint PrivateExtractIcons(string file, int index, int width, int height,
                                         IntPtr[] icons, uint[] ids, uint count, uint flags);
  [DllImport("user32.dll")]
  static extern bool DestroyIcon(IntPtr icon);

  public static string Png(string path) {
    var icons = new IntPtr[1];
    var ids = new uint[1];
    var count = PrivateExtractIcons(path, 0, 128, 128, icons, ids, 1, 0);
    if (count == 0 || count == UInt32.MaxValue || icons[0] == IntPtr.Zero) return null;
    try {
      using (var icon = (Icon)Icon.FromHandle(icons[0]).Clone())
      using (var bitmap = icon.ToBitmap())
      using (var stream = new MemoryStream()) {
        bitmap.Save(stream, ImageFormat.Png);
        return Convert.ToBase64String(stream.ToArray());
      }
    } finally {
      DestroyIcon(icons[0]);
    }
  }
}
"@
Add-Type -TypeDefinition $code -ReferencedAssemblies System.Drawing
$out = @{}
foreach ($p in @({PATHS})) {
  try {
    $png = [DeckIcon]::Png($p)
    if (-not $png) {
      $ms = New-Object System.IO.MemoryStream
      [System.Drawing.Icon]::ExtractAssociatedIcon($p).ToBitmap().Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)
      $png = [Convert]::ToBase64String($ms.ToArray())
    }
    $out[$p] = $png
  } catch { }
}
ConvertTo-Json -InputObject $out -Compress
"""

KNOWN_FOLDERS = {
    "{6D809377-6AF0-444B-8957-A3773F02200E}": os.environ.get("ProgramW6432", ""),
    "{7C5A40EF-A0FB-4BFC-874A-C0F2E0B9FA8E}": os.environ.get("ProgramFiles(x86)", ""),
    "{905E63B6-C1BF-494E-B29C-65B732D3D21A}": os.environ.get("ProgramFiles", ""),
    "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}": os.path.join(os.environ.get("windir", ""), "System32"),
    "{D65231B0-B2F1-4857-A4CE-A8E7C6EA7D27}": os.path.join(os.environ.get("windir", ""), "System32"),
    "{F38BF404-1D43-42F2-9305-67DE0B28FC23}": os.environ.get("windir", ""),
    "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}": os.path.join(os.environ.get("USERPROFILE", ""), "Desktop"),
}

# ponytail: o icone vira URL propria (/i/<hash>.png) em vez de data: no JSON.
# O celular baixa cada PNG uma vez e reusa do cache do navegador; o payload das
# recargas cai de centenas de kB para poucos kB.
_icon_cache = {}
_icon_data = {}
_payload_lock = threading.Lock()
# ponytail: o celular manda o indice, nunca o caminho - mesma regra dos favoritos.
# Guardo a ultima lista servida pro indice nao apontar pra outro app.
_recents = []


def png_square(size, rgb):
    """PNG solido, sem dependencia externa. So serve de icone do atalho na home."""
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    scanline = b"\0" + bytes(rgb) * size
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(scanline * size, 9))
            + chunk(b"IEND", b""))


ICON = png_square(192, (0xE8, 0x93, 0x3A))

MANIFEST = json.dumps({
    "name": "Deck",
    "short_name": "Deck",
    "start_url": "/",
    "display": "fullscreen",
    "orientation": "landscape",
    "background_color": "#f2f5f8",
    "theme_color": "#f2f5f8",
    "icons": [{"src": "/icon.png", "sizes": "192x192", "type": "image/png"}],
}, separators=(",", ":")).encode()

IMMUTABLE = "public, max-age=31536000, immutable"
REVALIDATE = "no-cache"
NO_STORE = "no-store"


def etag_of(body):
    return '"%s"' % hashlib.blake2b(body, digest_size=8).hexdigest()


MANIFEST_ETAG = etag_of(MANIFEST)
MANIFEST_GZ = gzip.compress(MANIFEST, 9)
ICON_ETAG = etag_of(ICON)

_index = None


def index_asset():
    """(corpo, gzip, etag) do index.html, relido so quando o arquivo muda."""
    global _index
    path = os.path.join(BUNDLE_DIR, "index.html")
    stamp = os.stat(path).st_mtime_ns
    cached = _index
    if cached is None or cached[0] != stamp:
        with open(path, "rb") as f:
            body = f.read()
        cached = (stamp, body, gzip.compress(body, 9), etag_of(body))
        _index = cached
    return cached[1:]


def ps(script):
    r = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command",
         "[Console]::OutputEncoding = [Text.Encoding]::UTF8; " + script],
        capture_output=True, encoding="utf-8", errors="replace", timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if r.returncode:
        raise RuntimeError(r.stderr.strip() or "powershell falhou")
    return r.stdout.strip()


def favorites():
    try:
        with open(FAVS, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return []


# ponytail: sem lock. Um celular, um clique por vez. Se virar multi-usuario, trave o arquivo.
def save_favorites(favs):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(FAVS, "w", encoding="utf-8") as f:
        json.dump(favs, f, indent=2, ensure_ascii=False)


def main_windows():
    """(hwnd, pid) da janela principal de cada processo, na ordem do EnumWindows.

    Mesma regra do MainWindowHandle do .NET: primeira janela visivel e sem dono.
    """
    found = []
    seen = set()
    pid = wintypes.DWORD()
    ref = ctypes.byref(pid)

    def visit(hwnd, _):
        if _IsWindowVisible(hwnd) and not _GetWindow(hwnd, GW_OWNER):
            _GetWindowThreadProcessId(hwnd, ref)
            if pid.value not in seen:
                seen.add(pid.value)
                found.append((hwnd, pid.value))
        return True

    _EnumWindows(_ENUM_PROC(visit), 0)
    return found


def window_title(hwnd):
    length = _GetWindowTextLengthW(hwnd)
    if not length:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    _GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def process_path(pid, buf, size):
    handle = _OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size.value = len(buf)
        return buf.value if _QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)) else ""
    finally:
        _CloseHandle(handle)


def list_windows():
    buf = ctypes.create_unicode_buffer(4096)
    size = wintypes.DWORD()
    out = []
    for hwnd, pid in main_windows():
        title = window_title(hwnd)
        if not title:
            continue
        path = process_path(pid, buf, size)
        out.append({
            "Id": pid,
            "ProcessName": os.path.splitext(os.path.basename(path))[0] if path else title,
            "MainWindowTitle": title,
            "Path": path,
        })
    out.sort(key=lambda w: (w["ProcessName"].lower(), w["Id"]))
    return out


def icons_for(paths):
    missing = sorted({p for p in paths if p and p not in _icon_cache and os.path.isfile(p)})
    if missing:
        literals = ",".join("'" + p.replace("'", "''") + "'" for p in missing)
        got = json.loads(ps(PS_ICONS.replace("{PATHS}", literals)) or "{}")
        for p in missing:
            encoded = got.get(p)
            url = None
            if encoded:
                blob = base64.b64decode(encoded)
                name = hashlib.blake2b(blob, digest_size=8).hexdigest()
                _icon_data[name] = blob
                url = "/i/" + name + ".png"
            _icon_cache[p] = url
    return {p: _icon_cache[p] for p in paths if _icon_cache.get(p)}


# ponytail: o UserAssist e a propria lista de "mais usados" do menu Iniciar.
# Nomes em ROT13, ultimo uso em FILETIME no offset 60. Leio direto do registro.
def recents(favs):
    rows = []
    try:
        root = winreg.OpenKey(winreg.HKEY_CURRENT_USER, USER_ASSIST)
    except OSError:
        return []
    with root:
        for i in range(winreg.QueryInfoKey(root)[0]):
            try:
                with winreg.OpenKey(root, winreg.EnumKey(root, i) + r"\Count") as count:
                    for j in range(winreg.QueryInfoKey(count)[1]):
                        name, data, kind = winreg.EnumValue(count, j)
                        if kind != winreg.REG_BINARY or len(data) < 68:
                            continue
                        used = struct.unpack_from("<q", data, 60)[0]
                        if used:
                            rows.append((used, name))
            except OSError:
                continue
    rows.sort(key=lambda r: -r[0])

    seen = {a.get("path", "").lower() for a in favs}
    apps = []
    for _used, name in rows:
        path = codecs.encode(name, "rot13")
        if path[:1] == "{":
            path = KNOWN_FOLDERS.get(path[:38].upper(), "") + path[38:]
        low = path.lower()
        if not low.endswith(".exe") or low in seen or not os.path.isfile(path):
            continue
        seen.add(low)
        apps.append({"name": os.path.splitext(os.path.basename(path))[0], "path": path})
        if len(apps) == RECENT_MAX:
            break
    return apps


def apps_payload():
    global _recents
    with _payload_lock:
        favs = favorites()
        windows = list_windows()
        recent = _recents = recents(favs)
        icons = icons_for([a.get("path", "") for a in favs]
                          + [w["Path"] for w in windows]
                          + [a["path"] for a in recent])
        fav_paths = {a.get("path", "").lower() for a in favs}
        return {
            "favorites": [
                {"name": a["name"], "icon": icons.get(a.get("path", ""))}
                for a in favs
            ],
            "running": [
                {"pid": w["Id"],
                 "name": w["MainWindowTitle"],
                 "icon": icons.get(w["Path"]),
                 "fav": w["Path"].lower() in fav_paths}
                for w in windows
            ],
            "recent": [
                {"name": a["name"], "icon": icons.get(a["path"])}
                for a in recent
            ],
        }


# ponytail: o toque no ALT dribla o foreground lock do Windows. Sem ele o
# SetForegroundWindow devolve False e a janela abre atras das outras.
# Maximiza sempre - do celular nao da pra arrastar borda pra redimensionar,
# entao a janela pequena que o app abre por padrao so atrapalha.
def activate(hwnd):
    _keybd_event(VK_MENU, 0, 0, 0)
    _keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
    _ShowWindow(hwnd, SW_MAXIMIZE)
    _SetForegroundWindow(hwnd)


def focus(pid):
    pid = int(pid)
    for hwnd, owner in main_windows():
        if owner == pid:
            activate(hwnd)
            return


def launch(path):
    before = {hwnd for hwnd, _ in main_windows()}
    os.startfile(path)
    for _try in range(20):
        time.sleep(0.25)
        for hwnd, _pid in main_windows():
            if hwnd not in before:
                activate(hwnd)
                return


def run(req):
    if "pid" in req:
        return focus(req["pid"])
    if "recent" in req:
        path = _recents[int(req["recent"])]["path"]
    else:
        path = favorites()[int(req["fav"])]["path"]
    low = path.lower()
    win = next((w for w in list_windows() if w["Path"].lower() == low), None)
    if win:
        focus(win["Id"])
    else:
        launch(path)


def edit_favorites(req):
    favs = favorites()
    if "remove" in req:
        favs.pop(int(req["remove"]))
    elif "add_recent" in req:
        app = _recents[int(req["add_recent"])]
        if any(a.get("path", "").lower() == app["path"].lower() for a in favs):
            return
        favs.append({"name": app["name"], "path": app["path"]})
    else:
        pid = int(req["add"])
        win = next((w for w in list_windows() if w["Id"] == pid), None)
        if win is None:
            raise ValueError("processo %d nao esta mais aberto" % pid)
        if not win["Path"]:
            raise ValueError("sem caminho do executavel (processo elevado?) - adicione a mao no apps.json")
        if any(a.get("path", "").lower() == win["Path"].lower() for a in favs):
            return
        favs.append({"name": win["ProcessName"], "path": win["Path"]})
    save_favorites(favs)


class Deck(http.server.BaseHTTPRequestHandler):
    # HTTP/1.1 mantem a conexao viva: o celular pede o HTML, o JSON e dezenas de
    # icones sem refazer o handshake TCP a cada um.
    protocol_version = "HTTP/1.1"
    disable_nagle_algorithm = True

    def do_GET(self):
        path = self.path
        if path == "/api/apps":
            body = json.dumps(apps_payload(), separators=(",", ":")).encode()
            return self._send(body, "application/json", NO_STORE)
        if path.startswith("/i/"):
            blob = _icon_data.get(path[3:-4])
            if blob is None:
                return self.send_error(404)
            return self._send(blob, "image/png", IMMUTABLE)
        if path == "/":
            body, packed, etag = index_asset()
            return self._send(body, "text/html; charset=utf-8", REVALIDATE, etag, packed)
        if path == "/manifest.json":
            return self._send(MANIFEST, "application/manifest+json", REVALIDATE,
                              MANIFEST_ETAG, MANIFEST_GZ)
        if path == "/icon.png":
            return self._send(ICON, "image/png", REVALIDATE, ICON_ETAG)
        self.send_error(404)

    def do_POST(self):
        if self.path == "/api/run":
            action = run
        elif self.path == "/api/fav":
            action = edit_favorites
        else:
            return self.send_error(404)
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        try:
            action(json.loads(body or b"{}"))
        except Exception as e:
            # A linha de status vai como latin-1; texto do Windows pode fugir disso.
            reason = str(e).splitlines()[0][:200].encode("latin-1", "replace").decode("latin-1")
            return self.send_error(400, reason)
        self._send(b'{"ok":true}', "application/json", NO_STORE)

    def _send(self, body, ctype, cache=None, etag=None, packed=None):
        if etag and self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            if cache:
                self.send_header("Cache-Control", cache)
            self.end_headers()
            return
        encoding = None
        if "gzip" in (self.headers.get("Accept-Encoding") or ""):
            if packed is None and len(body) > 900 and not ctype.startswith("image/"):
                packed = gzip.compress(body, 1)
            if packed is not None:
                body, encoding = packed, "gzip"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if encoding:
            self.send_header("Content-Encoding", encoding)
        if cache:
            self.send_header("Cache-Control", cache)
        if etag:
            self.send_header("ETag", etag)
        self.end_headers()
        self.wfile.write(body)

    # O executavel instalado nao tem console. A implementacao padrao tenta
    # escrever em sys.stderr e gera uma excecao a cada requisicao nesse modo.
    def log_message(self, format, *args):
        if sys.stderr is not None:
            super().log_message(format, *args)


class DeckServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        if sys.stderr is not None:
            return super().handle_error(request, client_address)
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(os.path.join(DATA_DIR, "error.log"), "a", encoding="utf-8") as log:
            log.write("\n[%s] Erro atendendo %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), client_address[0]))
            traceback.print_exc(file=log)


def warm_up():
    """Extrai os icones em segundo plano: a 1a abertura no celular ja vem pronta."""
    def work():
        try:
            apps_payload()
        except Exception:
            pass

    threading.Thread(target=work, name="StreamDeck warmup", daemon=True).start()


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        try:
            addresses = socket.gethostbyname_ex(socket.gethostname())[2]
            return next(ip for ip in addresses if not ip.startswith("127."))
        except (OSError, StopIteration):
            return "127.0.0.1"
    finally:
        s.close()


def tray_image(size=256):
    """Icone geometrico legivel tanto na bandeja clara quanto na escura."""
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    margin = max(1, size // 16)
    radius = max(2, size // 5)
    draw.rounded_rectangle(
        (margin, margin, size - margin - 1, size - margin - 1),
        radius=radius,
        fill=(23, 33, 44, 255),
    )
    gap = max(1, size // 18)
    inner = size // 4
    key = (size - 2 * inner - gap) // 2
    colors = ((242, 245, 248, 255),) * 3 + ((232, 147, 58, 255),)
    positions = (
        (inner, inner),
        (inner + key + gap, inner),
        (inner, inner + key + gap),
        (inner + key + gap, inner + key + gap),
    )
    for (left, top), color in zip(positions, colors):
        draw.rounded_rectangle(
            (left, top, left + key, top + key),
            radius=max(1, size // 32),
            fill=color,
        )
    return image


def local_url():
    return "http://127.0.0.1:%d" % PORT


def phone_url():
    return "http://%s:%d" % (lan_ip(), PORT)


def deck_is_running():
    try:
        with urllib.request.urlopen(local_url() + "/manifest.json", timeout=1) as response:
            return json.load(response).get("name") == "Deck"
    except (OSError, ValueError):
        return False


def show_error(message):
    ctypes.windll.user32.MessageBoxW(None, message, "StreamDeck de bolso", 0x10)


def run_tray():
    import pystray

    try:
        server = DeckServer(("0.0.0.0", PORT), Deck)
    except OSError as error:
        if deck_is_running():
            webbrowser.open(local_url())
        else:
            show_error("Nao foi possivel iniciar o StreamDeck.\n\nA porta %d ja esta em uso.\n\n%s"
                       % (PORT, error))
        return

    address = phone_url()
    server_thread = threading.Thread(
        target=server.serve_forever,
        name="StreamDeck HTTP",
        daemon=True,
    )
    server_thread.start()
    warm_up()

    def open_local(icon, item):
        webbrowser.open(local_url())

    def copy_address(icon, item):
        try:
            ps("Set-Clipboard -Value '%s'" % address.replace("'", "''"))
            icon.notify("Endereco copiado: %s" % address, "StreamDeck de bolso")
        except Exception as error:
            show_error("Nao foi possivel copiar o endereco.\n\n%s" % error)

    def quit_app(icon, item):
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("Abrir neste computador", open_local, default=True),
        pystray.MenuItem("Copiar endereco do celular", copy_address),
        pystray.MenuItem(address, lambda icon, item: None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Sair do StreamDeck", quit_app),
    )
    icon = pystray.Icon(
        "StreamDeckDeBolso",
        tray_image(),
        "StreamDeck de bolso - %s" % address,
        menu,
    )

    def ready(tray_icon):
        tray_icon.visible = True
        try:
            tray_icon.notify(
                "Rodando em segundo plano.\nNo celular, acesse %s" % address,
                "StreamDeck de bolso",
            )
        except NotImplementedError:
            pass

    try:
        icon.run(setup=ready)
    finally:
        if server_thread.is_alive():
            server.shutdown()
            server_thread.join(timeout=2)
        server.server_close()


def serve_console():
    address = phone_url()
    print("Deck em  %s   (libere no firewall do Windows na 1a vez)" % address)
    warm_up()
    DeckServer(("0.0.0.0", PORT), Deck).serve_forever()


def foreground_owner():
    buf = ctypes.create_unicode_buffer(4096)
    size = wintypes.DWORD()
    pid = wintypes.DWORD()
    _GetWindowThreadProcessId(_GetForegroundWindow(), ctypes.byref(pid))
    return os.path.splitext(os.path.basename(process_path(pid.value, buf, size)))[0]


def selfcheck():
    p = apps_payload()
    assert p["running"], "nenhuma janela encontrada - EnumWindows quebrou"
    assert all({"pid", "name", "icon", "fav"} <= set(w) for w in p["running"])
    assert all(isinstance(w["pid"], int) for w in p["running"])
    assert p["recent"], "nenhum app recente - UserAssist quebrou"
    assert all({"name", "icon"} <= set(a) for a in p["recent"])
    assert all(os.path.isfile(a["path"]) for a in _recents)
    assert all(_icon_data[u["icon"][3:-4]] for u in p["recent"] if u["icon"])

    probe = os.path.join(tempfile.gettempdir(), "deck-icon-check.png")
    try:
        with open(probe, "wb") as f:
            f.write(ICON)
        ps("Add-Type -AssemblyName System.Drawing;"
           " $i = [System.Drawing.Image]::FromFile('%s');"
           " if ($i.Width -ne 192 -or $i.Height -ne 192) { throw 'PNG do icone invalido' }" % probe)
    finally:
        os.path.exists(probe) and os.remove(probe)

    # ponytail: arma a trava de foreground - ativa a janela e bate um SHIFT nela.
    def arm(hwnd):
        activate(hwnd)
        time.sleep(0.4)
        _keybd_event(VK_SHIFT, 0, 0, 0)
        _keybd_event(VK_SHIFT, 0, KEYEVENTF_KEYUP, 0)

    def main_window(pid):
        return next((h for h, owner in main_windows() if owner == pid), None)

    if foreground_owner() == LOCK_SCREEN:
        print("aviso: sessao bloqueada, teste da trava de foreground pulado")
    else:
        other = list_windows()[0]
        other_h = main_window(other["Id"])
        invalido = "nao consegui armar a trava de foreground - teste invalido"

        arm(other_h)
        assert _GetForegroundWindow() == other_h, invalido
        launch("notepad.exe")
        pad = next(w for w in list_windows() if os.path.basename(w["Path"]).lower() == "notepad.exe")
        handle = main_window(pad["Id"])
        try:
            time.sleep(0.6)
            assert _GetForegroundWindow() == handle, "app recem-lancado ficou atras (trava de foreground)"
            arm(other_h)
            assert _GetForegroundWindow() == other_h, invalido
            focus(pad["Id"])
            time.sleep(0.6)
            assert _GetForegroundWindow() == handle, "janela existente ficou atras (trava de foreground)"
        finally:
            ps("Stop-Process -Id %d -Force" % pad["Id"])

    print("ok: %d abertos, %d favoritos, %d recentes, %d icones, manifest %d bytes, icon %d bytes"
          % (len(p["running"]), len(p["favorites"]), len(p["recent"]),
             len(_icon_data), len(MANIFEST), len(ICON)))


if __name__ == "__main__":
    if "check" in sys.argv:
        selfcheck()
    elif "serve" in sys.argv:
        serve_console()
    else:
        run_tray()
