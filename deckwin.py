"""Backend Windows.

Janelas, foco, lancamento e recentes falam direto com o Win32 e com o registro.
A versao antiga abria um powershell.exe por acao - e o Add-Type ainda chamava o
compilador de C# a cada toque, o que custava segundos.
"""
import base64
import codecs
import ctypes
import json
import os
import struct
import subprocess
import sys
import time
import winreg
from ctypes import wintypes

# ponytail: com a sessao bloqueada esse processo segura o foreground e ninguem
# toma o lugar dele - a trava so da pra testar com a area de trabalho aberta.
LOCK_SCREEN = "LockApp"
USER_ASSIST = r"Software\Microsoft\Windows\CurrentVersion\Explorer\UserAssist"
RECENT_MAX = int(os.environ.get("DECK_RECENT_MAX", 24))

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
            "id": pid,
            "app": os.path.splitext(os.path.basename(path))[0] if path else title,
            "title": title,
            "path": path,
        })
    out.sort(key=lambda w: (w["app"].lower(), w["id"]))
    return out


def icon_bytes(paths):
    """{caminho do executavel: (extensao, bytes da imagem)}."""
    real = sorted(p for p in paths if os.path.isfile(p))
    if not real:
        return {}
    literals = ",".join("'" + p.replace("'", "''") + "'" for p in real)
    got = json.loads(ps(PS_ICONS.replace("{PATHS}", literals)) or "{}")
    return {p: ("png", base64.b64decode(got[p])) for p in real if got.get(p)}


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


# ponytail: o toque no ALT dribla o foreground lock do Windows. Sem ele o
# SetForegroundWindow devolve False e a janela abre atras das outras.
# Maximiza sempre - do celular nao da pra arrastar borda pra redimensionar,
# entao a janela pequena que o app abre por padrao so atrapalha.
def activate(hwnd):
    _keybd_event(VK_MENU, 0, 0, 0)
    _keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
    _ShowWindow(hwnd, SW_MAXIMIZE)
    _SetForegroundWindow(hwnd)


def focus(window_id):
    pid = int(window_id)
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


def copy_text(text):
    ps("Set-Clipboard -Value '%s'" % text.replace("'", "''"))


def show_error(message):
    ctypes.windll.user32.MessageBoxW(None, message, "StreamDeck de bolso", 0x10)


def foreground_owner():
    buf = ctypes.create_unicode_buffer(4096)
    size = wintypes.DWORD()
    pid = wintypes.DWORD()
    _GetWindowThreadProcessId(_GetForegroundWindow(), ctypes.byref(pid))
    return os.path.splitext(os.path.basename(process_path(pid.value, buf, size)))[0]


def selfcheck():
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
        return
    other = list_windows()[0]
    other_h = main_window(other["id"])
    invalido = "nao consegui armar a trava de foreground - teste invalido"

    arm(other_h)
    assert _GetForegroundWindow() == other_h, invalido
    launch("notepad.exe")
    pad = next(w for w in list_windows() if os.path.basename(w["path"]).lower() == "notepad.exe")
    handle = main_window(pad["id"])
    try:
        time.sleep(0.6)
        assert _GetForegroundWindow() == handle, "app recem-lancado ficou atras (trava de foreground)"
        arm(other_h)
        assert _GetForegroundWindow() == other_h, invalido
        focus(pad["id"])
        time.sleep(0.6)
        assert _GetForegroundWindow() == handle, "janela existente ficou atras (trava de foreground)"
    finally:
        ps("Stop-Process -Id %d -Force" % pad["id"])
    print("ok: trava de foreground vencida no lancamento e no foco")


assert sys.platform == "win32", "deckwin so roda no Windows"
