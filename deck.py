"""StreamDeck de bolso.

Uso:
    python deck.py           inicia o servidor, imprime a URL pra abrir no celular
    python deck.py check     autoteste

No celular: abre a URL no Chrome, menu -> "Adicionar a tela inicial". O atalho
abre em tela cheia, sem barra de navegador.

Favoritos ficam em apps.json (name + path). path aceita .exe, .lnk, pasta ou URL.
Da pra adicionar e remover favoritos pelo proprio celular (botao de editar).
Porta: variavel de ambiente DECK_PORT (padrao 8765).
"""
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

PS_LIST = r"""
$ErrorActionPreference = 'SilentlyContinue'
$w = Get-Process | Where-Object { $_.MainWindowTitle } |
     Select-Object Id, ProcessName, MainWindowTitle, @{n='Path';e={$_.Path}}
ConvertTo-Json -InputObject @($w) -Depth 2 -Compress
"""

PS_ICONS = r"""
Add-Type -AssemblyName System.Drawing
$out = @{}
foreach ($p in @({PATHS})) {
  try {
    $ms = New-Object System.IO.MemoryStream
    [System.Drawing.Icon]::ExtractAssociatedIcon($p).ToBitmap().Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)
    $out[$p] = [Convert]::ToBase64String($ms.ToArray())
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

_icon_cache = {}


def png_square(size, rgb):
    """PNG solido, sem dependencia externa. So serve de icone do atalho na home."""
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    scanline = b"\0" + bytes(rgb) * size
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(scanline * size, 9))
            + chunk(b"IEND", b""))


ICON = png_square(192, (0x33, 0x47, 0x5C))

MANIFEST = json.dumps({
    "name": "Deck",
    "short_name": "Deck",
    "start_url": "/",
    "display": "fullscreen",
    "background_color": "#111111",
    "theme_color": "#111111",
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


def apps_payload():
    favs, windows = favorites(), list_windows()
    icons = icons_for([a.get("path", "") for a in favs] + [w.get("Path") or "" for w in windows])
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
    }


def focus(pid):
    ps(PS_FOCUS.replace("{PID}", str(int(pid))))


def launch(path):
    ps(PS_LAUNCH.replace("{PATH}", path.replace("'", "''")))


def run(req):
    if "pid" in req:
        return focus(req["pid"])
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

    print("ok: %d abertos, %d favoritos, %d icones, manifest %d bytes, icon %d bytes"
          % (len(p["running"]), len(p["favorites"]),
             sum(1 for v in _icon_cache.values() if v), len(MANIFEST), len(ICON)))


if __name__ == "__main__":
    if "check" in sys.argv:
        selfcheck()
    else:
        print("Deck em  http://%s:%d   (libere no firewall do Windows na 1a vez)" % (lan_ip(), PORT))
        http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Deck).serve_forever()
