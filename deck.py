"""StreamDeck de bolso.

Uso:
    python deck.py           inicia na bandeja do sistema
    python deck.py serve     inicia no terminal e imprime a URL
    python deck.py check     autoteste

No celular: abre a URL no Chrome, menu -> Compartilhar -> "Adicionar a tela
inicial". O atalho abre em tela cheia, deitado.

Gestos: desliza pro lado troca a pagina de favoritos, pra cima mostra os apps
abertos, pra baixo os usados recentemente. Toque abre, toque longo adiciona ou
remove dos favoritos.

Favoritos ficam em apps.json (name + path). No Windows path aceita .exe, .lnk,
pasta ou URL; no Linux, um .desktop, pasta ou URL. Em vez de path, um favorito
pode ter keys ("ctrl+shift+m"): o toque manda esse atalho pro teclado do PC.
Com mais de um monitor da pra escolher em qual as janelas abrem (settings.json).
Porta: variavel de ambiente DECK_PORT (padrao 8765).
"""
import gzip
import hashlib
import http.server
import json
import os
import socket
import struct
import sys
import threading
import time
import traceback
import urllib.request
import webbrowser
import zlib

# Tudo que fala com o sistema operacional (janelas, foco, lancamento, icones)
# mora no backend. O resto deste arquivo e igual nos dois.
if sys.platform == "win32":
    import deckwin as backend
else:
    import decklinux as backend

# O PyInstaller extrai os arquivos empacotados em ``sys._MEIPASS``. A
# configuracao, por outro lado, precisa ficar fora da pasta do programa para
# continuar gravavel depois da instalacao e sobreviver a atualizacoes.
BUNDLE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
if not getattr(sys, "frozen", False):
    DATA_DIR = BUNDLE_DIR
elif sys.platform == "win32":
    DATA_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "StreamDeck")
else:
    DATA_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME",
                                           os.path.expanduser("~/.config")), "StreamDeck")
FAVS = os.path.join(DATA_DIR, "apps.json")
SETTINGS = os.path.join(DATA_DIR, "settings.json")
PORT = int(os.environ.get("DECK_PORT", 8765))

# Atalhos de teclado ---------------------------------------------------------
# O vocabulario de teclas mora aqui, nao no backend: os dois sistemas aceitam os
# mesmos nomes e cada backend so traduz pro codigo dele. Assim um apps.json
# escrito no Windows continua valendo no Linux.
MODIFIERS = ("ctrl", "shift", "alt", "win")
MOD_ALIASES = {"control": "ctrl", "ctl": "ctrl", "super": "win", "meta": "win",
               "cmd": "win", "command": "win", "windows": "win", "logo": "win",
               "option": "alt"}
KEY_ALIASES = {"return": "enter", "escape": "esc", "del": "delete", "ins": "insert",
               "pgup": "pageup", "pgdn": "pagedown", "prtsc": "printscreen",
               "print": "printscreen", "spacebar": "space", "volup": "volumeup",
               "voldown": "volumedown", "volumemute": "mute", "play": "playpause",
               "pause": "playpause", "nexttrack": "next", "prevtrack": "prev",
               "previous": "prev", "quote": "apostrophe", "dot": "period"}
NAMED_KEYS = ("enter esc tab space backspace delete insert home end pageup pagedown "
              "up down left right printscreen menu capslock "
              "volumeup volumedown mute playpause next prev stop "
              "comma period slash backslash semicolon apostrophe grave "
              "bracketleft bracketright minus equal").split()
KEY_NAMES = (set(NAMED_KEYS) | set("abcdefghijklmnopqrstuvwxyz0123456789")
             | {"f%d" % n for n in range(1, 25)})


def parse_hotkey(combo):
    """"Ctrl+Shift+M" -> (["ctrl", "shift"], "m"). Levanta ValueError se nao existir."""
    parts = [p.strip().lower() for p in str(combo).split("+") if p.strip()]
    if not parts:
        raise ValueError("atalho vazio")
    if len(parts) > 5:
        raise ValueError("atalho com modificadores demais")
    key = KEY_ALIASES.get(parts[-1], parts[-1])
    if key not in KEY_NAMES:
        raise ValueError("tecla desconhecida: %s" % parts[-1])
    mods = []
    for raw in parts[:-1]:
        mod = MOD_ALIASES.get(raw, raw)
        if mod not in MODIFIERS:
            raise ValueError("modificador desconhecido: %s" % raw)
        if mod not in mods:
            mods.append(mod)
    # Ordem canonica: dois atalhos iguais viram a mesma string e nao duplicam.
    mods.sort(key=MODIFIERS.index)
    return mods, key


def hotkey_text(combo):
    mods, key = parse_hotkey(combo)
    return "+".join(mods + [key])


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

# ponytail: o icone vira URL propria (/i/<hash>.png) em vez de data: no JSON.
# O celular baixa cada imagem uma vez e reusa do cache do navegador; o payload
# das recargas cai de centenas de kB para poucos kB.
_icon_cache = {}
_icon_data = {}
_payload_lock = threading.Lock()
# ponytail: o celular manda o indice, nunca o caminho - mesma regra dos favoritos.
# Guardo a ultima lista servida pro indice nao apontar pra outro app.
_recents = []
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


def settings():
    try:
        with open(SETTINGS, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def save_settings(cfg):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(SETTINGS, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def target_screen():
    """Id da tela que o deck comanda, ou None pra deixar o sistema decidir.

    Tela desconectada vale como None: melhor abrir onde der do que nao abrir.
    """
    chosen = settings().get("screen")
    if not chosen:
        return None
    return chosen if any(s["id"] == chosen for s in backend.list_screens()) else None


def choose_screen(req):
    screen = req.get("screen") or None
    if screen and not any(s["id"] == screen for s in backend.list_screens()):
        raise ValueError("essa tela nao esta conectada")
    cfg = settings()
    cfg["screen"] = screen
    save_settings(cfg)


def icons_for(paths):
    """{caminho: URL do icone}. Extrai uma vez por app e serve do cache depois."""
    missing = sorted({p for p in paths if p and p not in _icon_cache})
    if missing:
        got = backend.icon_bytes(missing)
        for p in missing:
            found = got.get(p)
            url = None
            if found:
                ext, blob = found
                name = "%s.%s" % (hashlib.blake2b(blob, digest_size=8).hexdigest(), ext)
                _icon_data[name] = blob
                url = "/i/" + name
            _icon_cache[p] = url
    return {p: _icon_cache[p] for p in paths if _icon_cache.get(p)}


def same(a, b):
    """Windows nao diferencia maiuscula no caminho; Linux sim, mas casefold nao atrapalha."""
    return a.lower() == b.lower()


def fav_item(app, icons):
    """Favorito como o celular ve: so leva keys quando for atalho de teclado."""
    item = {"name": app["name"], "icon": icons.get(app.get("path", ""))}
    if app.get("keys"):
        item["keys"] = app["keys"]
    return item


def apps_payload():
    global _recents
    with _payload_lock:
        favs = favorites()
        windows = backend.list_windows()
        recent = _recents = backend.recents(favs)
        icons = icons_for([a.get("path", "") for a in favs]
                          + [w["path"] for w in windows]
                          + [a["path"] for a in recent])
        fav_paths = {a.get("path", "").lower() for a in favs}
        chosen = settings().get("screen")
        return {
            "screens": [{"id": s["id"], "name": s["name"], "w": s["w"], "h": s["h"],
                          "on": s["id"] == chosen} for s in backend.list_screens()],
            "favorites": [fav_item(a, icons) for a in favs],
            "running": [
                {"pid": w["id"],
                 "name": w["title"],
                 "icon": icons.get(w["path"]),
                 "fav": w["path"].lower() in fav_paths}
                for w in windows
            ],
            "recent": [
                {"name": a["name"], "icon": icons.get(a["path"])}
                for a in recent
            ],
        }


def run(req):
    screen = target_screen()
    if "pid" in req:
        return backend.focus(req["pid"], screen)
    if "recent" in req:
        path = _recents[int(req["recent"])]["path"]
    else:
        fav = favorites()[int(req["fav"])]
        if fav.get("keys"):
            # O atalho vai pra janela que estiver em foco no PC - nao abre nada.
            return backend.send_keys(*parse_hotkey(fav["keys"]))
        path = fav.get("path", "")
        if not path:
            raise ValueError("favorito sem path nem keys no apps.json")
    win = next((w for w in backend.list_windows() if w["path"] and same(w["path"], path)), None)
    if win:
        backend.focus(win["id"], screen)
    else:
        backend.launch(path, screen)


def edit_favorites(req):
    favs = favorites()
    if "remove" in req:
        favs.pop(int(req["remove"]))
    elif "hotkey" in req:
        combo = hotkey_text(req["hotkey"])
        if any(a.get("keys", "").lower() == combo for a in favs):
            return
        favs.append({"name": (req.get("name") or "").strip() or combo.upper(),
                     "keys": combo})
    elif "add_recent" in req:
        app = _recents[int(req["add_recent"])]
        if any(same(a.get("path", ""), app["path"]) for a in favs):
            return
        favs.append({"name": app["name"], "path": app["path"]})
    else:
        window_id = req["add"]
        win = next((w for w in backend.list_windows() if w["id"] == window_id), None)
        if win is None:
            raise ValueError("essa janela nao esta mais aberta")
        if not win["path"]:
            raise ValueError("sem caminho do aplicativo - adicione a mao no apps.json")
        if any(same(a.get("path", ""), win["path"]) for a in favs):
            return
        favs.append({"name": win["app"], "path": win["path"]})
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
            blob = _icon_data.get(path[3:])
            if blob is None:
                return self.send_error(404)
            ctype = "image/svg+xml" if path.endswith(".svg") else "image/png"
            return self._send(blob, ctype, IMMUTABLE)
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
        elif self.path == "/api/screen":
            action = choose_screen
        else:
            return self.send_error(404)
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        try:
            action(json.loads(body or b"{}"))
        except Exception as e:
            # A linha de status vai como latin-1; texto do sistema pode fugir disso.
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
            if packed is None and len(body) > 900 and ctype.startswith(("text/", "application/",
                                                                       "image/svg")):
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


def run_tray():
    try:
        import pystray
    except ImportError:
        # No Linux a bandeja depende de pystray + AppIndicator. Sem eles o modo
        # terminal cobre tudo (e e o que o servico do systemd usa mesmo).
        print("pystray nao instalado - caindo pro modo terminal")
        return serve_console()

    try:
        server = DeckServer(("0.0.0.0", PORT), Deck)
    except OSError as error:
        if deck_is_running():
            webbrowser.open(local_url())
        else:
            backend.show_error("Nao foi possivel iniciar o StreamDeck.\n\n"
                               "A porta %d ja esta em uso.\n\n%s" % (PORT, error))
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
            backend.copy_text(address)
            icon.notify("Endereco copiado: %s" % address, "StreamDeck de bolso")
        except Exception as error:
            backend.show_error("Nao foi possivel copiar o endereco.\n\n%s" % error)

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
    print("Deck em  %s   (libere a porta %d no firewall na 1a vez)" % (address, PORT))
    warm_up()
    DeckServer(("0.0.0.0", PORT), Deck).serve_forever()


def selfcheck():
    p = apps_payload()
    assert p["running"], "nenhuma janela encontrada"
    assert all({"pid", "name", "icon", "fav"} <= set(w) for w in p["running"])
    assert p["recent"], "nenhum app na lista de recentes/instalados"
    assert all({"name", "icon"} <= set(a) for a in p["recent"])
    assert all(os.path.exists(a["path"]) for a in _recents)
    assert all(_icon_data[u["icon"][3:]] for u in p["recent"] if u["icon"])
    assert any(u["icon"] for u in p["recent"]), "nenhum icone extraido"

    # Sem telas na lista o deck so perde a escolha de monitor - quem cobra isso e
    # o autoteste do backend, que sabe se o sistema devia ter respondido.
    screens = backend.list_screens()
    assert all({"id", "name", "w", "h"} <= set(s) for s in screens)
    assert len(p["screens"]) == len(screens)
    assert sum(s["on"] for s in p["screens"]) <= 1, "duas telas marcadas como a escolhida"

    assert KEY_NAMES <= backend.KEYS, ("teclas sem traducao no backend: %s"
                                      % sorted(KEY_NAMES - backend.KEYS))
    assert parse_hotkey("Ctrl+Shift+M") == (["ctrl", "shift"], "m")
    assert parse_hotkey(" super + f5 ") == (["win"], "f5")
    assert hotkey_text("shift+ctrl+volup") == "ctrl+shift+volumeup"
    for bad in ("", "ctrl+", "hyper+a", "ctrl+banana"):
        try:
            parse_hotkey(bad)
        except ValueError:
            continue
        raise AssertionError("parse_hotkey aceitou %r" % bad)

    print("ok: %d abertos, %d favoritos, %d recentes, %d icones, manifest %d bytes, icon %d bytes"
          % (len(p["running"]), len(p["favorites"]), len(p["recent"]),
             len(_icon_data), len(MANIFEST), len(ICON)))
    print("ok: telas %s, comandando %s"
          % (", ".join("%s (%dx%d)" % (s["name"], s["w"], s["h"]) for s in screens) or "(nenhuma)",
             target_screen() or "a que o sistema escolher"))
    backend.selfcheck()


if __name__ == "__main__":
    if "check" in sys.argv:
        selfcheck()
    elif "serve" in sys.argv:
        serve_console()
    else:
        run_tray()
