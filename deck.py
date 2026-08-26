"""StreamDeck de bolso.

Uso:
    python deck.py           inicia o servidor, imprime a URL pra abrir no celular
    python deck.py check     autoteste

No celular: abre a URL no Chrome, menu -> Compartilhar -> "Adicionar a tela
inicial". O atalho abre em tela cheia, deitado.

Gestos: desliza pro lado troca a pagina de favoritos, pra cima mostra os apps
abertos, pra baixo os usados recentemente. Toque abre, toque longo adiciona ou
remove dos favoritos.

Favoritos ficam em apps.json (name + path). path aceita .exe, .lnk, pasta ou URL.
Porta: variavel de ambiente DECK_PORT (padrao 8765).
"""
import codecs
import http.server
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import time
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
FAVS = os.path.join(HERE, "apps.json")
PORT = int(os.environ.get("DECK_PORT", 8765))
RECENT_MAX = int(os.environ.get("DECK_RECENT_MAX", 24))
# ponytail: com a sessao bloqueada esse processo segura o foreground e ninguem
# toma o lugar dele - a trava so da pra testar com a area de trabalho aberta.
LOCK_SCREEN = "LockApp"

PS_LIST = r"""
$ErrorActionPreference = 'SilentlyContinue'
$w = Get-Process | Where-Object { $_.MainWindowTitle } |
     Select-Object Id, ProcessName, MainWindowTitle, @{n='Path';e={$_.Path}}
ConvertTo-Json -InputObject @($w) -Depth 2 -Compress
"""

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
    var count = PrivateExtractIcons(path, 0, 256, 256, icons, ids, 1, 0);
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

PS_WIN32 = r"""
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class Deck {
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int cmd);
  [DllImport("user32.dll")] public static extern void keybd_event(byte k, byte s, uint f, UIntPtr e);
  [DllImport("user32.dll")] public static extern int GetWindowThreadProcessId(IntPtr h, out int pid);
}
"@
"""

# ponytail: o toque no ALT dribla o foreground lock do Windows. Sem ele o
# SetForegroundWindow devolve False e a janela abre atras das outras.
PS_ACTIVATE = r"""
[Deck]::keybd_event(0x12, 0, 0, [UIntPtr]::Zero)
[Deck]::keybd_event(0x12, 0, 2, [UIntPtr]::Zero)
[Deck]::ShowWindow($h, 9) | Out-Null
[Deck]::SetForegroundWindow($h) | Out-Null
"""

PS_FOCUS = PS_WIN32 + "$h = (Get-Process -Id {PID}).MainWindowHandle" + PS_ACTIVATE

PS_FGOWNER = PS_WIN32 + r"""
$owner = 0
[Deck]::GetWindowThreadProcessId([Deck]::GetForegroundWindow(), [ref]$owner) | Out-Null
(Get-Process -Id $owner).ProcessName
"""

PS_LAUNCH = PS_WIN32 + r"""
$before = @(Get-Process | Where-Object { $_.MainWindowHandle } | ForEach-Object { $_.MainWindowHandle })
Start-Process -FilePath '{PATH}'
foreach ($try in 1..20) {
  Start-Sleep -Milliseconds 250
  $h = @(Get-Process | Where-Object { $_.MainWindowHandle -and $before -notcontains $_.MainWindowHandle } |
         ForEach-Object { $_.MainWindowHandle })[0]
  if ($h) {""" + PS_ACTIVATE + """    break
  }
}
"""

# ponytail: o UserAssist e a propria lista de "mais usados" do menu Iniciar.
# Nomes em ROT13, ultimo uso em FILETIME no offset 60. Decodifico no Python.
PS_RECENT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$out = @()
foreach ($s in Get-ChildItem 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\UserAssist') {
  $c = Join-Path $s.PSPath 'Count'
  if (-not (Test-Path $c)) { continue }
  $p = Get-ItemProperty $c
  foreach ($n in $p.PSObject.Properties.Name) {
    if ($n -like 'PS*') { continue }
    $d = $p.$n
    if ($d -isnot [byte[]] -or $d.Length -lt 68) { continue }
    $out += [pscustomobject]@{ n = $n; t = [BitConverter]::ToInt64($d, 60) }
  }
}
ConvertTo-Json -InputObject @($out) -Compress
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

_icon_cache = {}
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
}).encode()


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
    with open(FAVS, "w", encoding="utf-8") as f:
        json.dump(favs, f, indent=2, ensure_ascii=False)


def list_windows():
    out = ps(PS_LIST)
    data = json.loads(out) if out else []
    if isinstance(data, dict):
        data = [data]
    return sorted(data or [], key=lambda w: (w["ProcessName"] or "").lower())


def icons_for(paths):
    missing = sorted({p for p in paths if p and p not in _icon_cache and os.path.isfile(p)})
    if missing:
        literals = ",".join("'" + p.replace("'", "''") + "'" for p in missing)
        got = json.loads(ps(PS_ICONS.replace("{PATHS}", literals)) or "{}")
        for p in missing:
            _icon_cache[p] = got.get(p)
    return {p: "data:image/png;base64," + _icon_cache[p] for p in paths if _icon_cache.get(p)}


def recents():
    out = ps(PS_RECENT)
    rows = json.loads(out) if out else []
    if isinstance(rows, dict):
        rows = [rows]
    seen = {a.get("path", "").lower() for a in favorites()}
    apps = []
    for row in sorted(rows or [], key=lambda r: -(r["t"] or 0)):
        if not row["t"]:
            continue
        path = codecs.encode(row["n"], "rot13")
        if path[:1] == "{":
            path = KNOWN_FOLDERS.get(path[:38].upper(), "") + path[38:]
        if not path.lower().endswith(".exe") or path.lower() in seen:
            continue
        if not os.path.isfile(path):
            continue
        seen.add(path.lower())
        apps.append({"name": os.path.splitext(os.path.basename(path))[0], "path": path})
        if len(apps) == RECENT_MAX:
            break
    return apps


def apps_payload():
    global _recents
    favs, windows = favorites(), list_windows()
    _recents = recents()
    icons = icons_for([a.get("path", "") for a in favs]
                      + [w.get("Path") or "" for w in windows]
                      + [a["path"] for a in _recents])
    fav_paths = {a.get("path", "").lower() for a in favs}
    return {
        "favorites": [
            {"name": a["name"], "icon": icons.get(a.get("path", ""))}
            for a in favs
        ],
        "running": [
            {"pid": w["Id"],
             "name": w["MainWindowTitle"] or w["ProcessName"],
             "icon": icons.get(w.get("Path") or ""),
             "fav": (w.get("Path") or "").lower() in fav_paths}
            for w in windows
        ],
        "recent": [
            {"name": a["name"], "icon": icons.get(a["path"])}
            for a in _recents
        ],
    }


def focus(pid):
    ps(PS_FOCUS.replace("{PID}", str(int(pid))))


def launch(path):
    ps(PS_LAUNCH.replace("{PATH}", path.replace("'", "''")))


def run(req):
    if "pid" in req:
        return focus(req["pid"])
    if "recent" in req:
        path = _recents[int(req["recent"])]["path"]
    else:
        path = favorites()[int(req["fav"])]["path"]
    win = next((w for w in list_windows() if (w.get("Path") or "").lower() == path.lower()), None)
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
        if not win.get("Path"):
            raise ValueError("sem caminho do executavel (processo elevado?) - adicione a mao no apps.json")
        if any(a.get("path", "").lower() == win["Path"].lower() for a in favs):
            return
        favs.append({"name": win["ProcessName"], "path": win["Path"]})
    save_favorites(favs)


class Deck(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/apps":
            return self._send(json.dumps(apps_payload()).encode(), "application/json")
        if self.path == "/manifest.json":
            return self._send(MANIFEST, "application/manifest+json")
        if self.path == "/icon.png":
            return self._send(ICON, "image/png")
        if self.path == "/":
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                return self._send(f.read(), "text/html; charset=utf-8")
        self.send_error(404)

    def do_POST(self):
        actions = {"/api/run": run, "/api/fav": edit_favorites}
        if self.path not in actions:
            return self.send_error(404)
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        try:
            actions[self.path](json.loads(body or b"{}"))
        except Exception as e:
            return self.send_error(400, str(e).splitlines()[0][:200])
        self._send(b'{"ok":true}', "application/json")

    def _send(self, body, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()


def selfcheck():
    p = apps_payload()
    assert p["running"], "nenhuma janela encontrada - PS_LIST quebrou"
    assert all({"pid", "name", "icon", "fav"} <= set(w) for w in p["running"])
    assert all(isinstance(w["pid"], int) for w in p["running"])
    assert p["recent"], "nenhum app recente - PS_RECENT quebrou"
    assert all({"name", "icon"} <= set(a) for a in p["recent"])
    assert all(os.path.isfile(a["path"]) for a in _recents)

    probe = os.path.join(tempfile.gettempdir(), "deck-icon-check.png")
    try:
        with open(probe, "wb") as f:
            f.write(ICON)
        ps("Add-Type -AssemblyName System.Drawing;"
           " $i = [System.Drawing.Image]::FromFile('%s');"
           " if ($i.Width -ne 192 -or $i.Height -ne 192) { throw 'PNG do icone invalido' }" % probe)
    finally:
        os.path.exists(probe) and os.remove(probe)

    fg = PS_WIN32 + "[Deck]::GetForegroundWindow()"
    arm = PS_WIN32 + "$h = (Get-Process -Id {PID}).MainWindowHandle" + PS_ACTIVATE + r"""
Start-Sleep -Milliseconds 400
[Deck]::keybd_event(0x10, 0, 0, [UIntPtr]::Zero)
[Deck]::keybd_event(0x10, 0, 2, [UIntPtr]::Zero)
"""
    if ps(PS_FGOWNER) == LOCK_SCREEN:
        print("aviso: sessao bloqueada, teste da trava de foreground pulado")
    else:
        other = list_windows()[0]
        other_h = int(ps("(Get-Process -Id %d).MainWindowHandle" % other["Id"]))
        invalido = "nao consegui armar a trava de foreground - teste invalido"

        ps(arm.replace("{PID}", str(other["Id"])))
        assert int(ps(fg)) == other_h, invalido
        launch("notepad.exe")
        pid = int(ps("(Get-Process notepad | Sort-Object StartTime | Select-Object -Last 1).Id"))
        handle = int(ps("(Get-Process -Id %d).MainWindowHandle" % pid))
        try:
            time.sleep(0.6)
            assert int(ps(fg)) == handle, "app recem-lancado ficou atras (trava de foreground)"
            ps(arm.replace("{PID}", str(other["Id"])))
            assert int(ps(fg)) == other_h, invalido
            focus(pid)
            time.sleep(0.6)
            assert int(ps(fg)) == handle, "janela existente ficou atras (trava de foreground)"
        finally:
            ps("Stop-Process -Id %d -Force" % pid)

    print("ok: %d abertos, %d favoritos, %d recentes, %d icones, manifest %d bytes, icon %d bytes"
          % (len(p["running"]), len(p["favorites"]), len(p["recent"]),
             sum(1 for v in _icon_cache.values() if v), len(MANIFEST), len(ICON)))


if __name__ == "__main__":
    if "check" in sys.argv:
        selfcheck()
    else:
        print("Deck em  http://%s:%d   (libere no firewall do Windows na 1a vez)" % (lan_ip(), PORT))
        http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Deck).serve_forever()
