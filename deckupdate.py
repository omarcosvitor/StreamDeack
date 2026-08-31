"""Atualizacao do StreamDeck de bolso, direto das releases do GitHub.

O programa instalado sabe a propria versao pelo ``version.txt`` que o build
grava ao lado dele. O clone de desenvolvimento nao tem esse arquivo: ali o
updater so conta qual e a ultima versao publicada e nao mexe em nada.

O repositorio e publico, entao o pacote vem pela URL aberta da release e nenhuma
credencial e usada. Se um dia ele fechar (ou se os 60 pedidos por hora da API
anonima apertarem), da pra por um token de leitura em ``settings.json`` ->
``"github_token"``, ou na variavel STREAMDECK_TOKEN: ai o download passa a usar
a URL da API, que e a unica que aceita autenticacao.

Nada disso roda sozinho - so quando a bandeja, o celular ou o
``python deck.py update`` pedem. O deck nunca fala com a internet por conta
propria.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request

REPO = "omarcosvitor/StreamDeack"
LATEST_URL = "https://api.github.com/repos/%s/releases/latest" % REPO
AGENT = "StreamDeck-de-bolso"
TIMEOUT = 20
SEMVER = re.compile(r"\d+\.\d+\.\d+")

# Mesma conta do deck.py: no executavel congelado os arquivos ficam no _MEIPASS,
# no Linux ao lado do proprio codigo. O version.txt viaja junto do programa.
BUNDLE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


class _DropAuthOnRedirect(urllib.request.HTTPRedirectHandler):
    """Tira o Authorization ao pular pro storage do GitHub.

    O link do pacote responde com um redirecionamento pra um endereco ja
    assinado; se o token for junto, o storage recusa por ter duas autenticacoes.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Redirecionamento pra http seria baixar o pacote em claro: nem tenta.
        if not newurl.lower().startswith("https://"):
            raise urllib.error.URLError("o GitHub redirecionou pra fora do https")
        novo = super().redirect_request(req, fp, code, msg, headers, newurl)
        if novo is not None:
            novo.headers.pop("Authorization", None)
        return novo


_opener = urllib.request.build_opener(_DropAuthOnRedirect)


def _get(url, headers, timeout):
    """GET com certificado conferido (o padrao do Python) e sem cair pra http."""
    if not url.lower().startswith("https://"):
        raise RuntimeError("endereco fora do https: %s" % url)
    return _opener.open(urllib.request.Request(url, headers=headers), timeout=timeout)


def installed_version():
    """Versao gravada pelo build, ou "" quando o deck roda do codigo-fonte."""
    try:
        with open(os.path.join(BUNDLE_DIR, "version.txt"), encoding="utf-8") as f:
            found = f.read().strip()
    except OSError:
        return ""
    return found if SEMVER.fullmatch(found) else ""


def as_tuple(version):
    return tuple(int(p) for p in version.split("."))


def is_newer(candidate, installed):
    return as_tuple(candidate) > as_tuple(installed)


def asset_name(version):
    """O nome que o workflow de release da ao pacote de cada sistema."""
    if sys.platform == "win32":
        return "StreamDeck-Setup-%s.exe" % version
    return "StreamDeck-%s-linux.tar.gz" % version


def headers(token="", accept="application/vnd.github+json"):
    feito = {"User-Agent": AGENT, "Accept": accept}
    if token:
        feito["Authorization"] = "Bearer %s" % token
    return feito


def latest_release(token="", timeout=TIMEOUT):
    """A ultima release publicada: versao, pacote deste sistema e sha256."""
    try:
        with _get(LATEST_URL, headers(token), timeout) as response:
            release = json.load(response)
    except urllib.error.HTTPError as error:
        if error.code in (401, 403, 404) and not token:
            raise RuntimeError("o GitHub respondeu %s - se o repositorio for privado, "
                               "ponha um github_token no settings.json" % error.code)
        raise RuntimeError("o GitHub respondeu %s ao procurar a ultima versao" % error.code)
    except (OSError, ValueError) as error:
        raise RuntimeError("nao deu pra falar com o GitHub (%s)" % error)

    version = str(release.get("tag_name", "")).lstrip("v")
    if not SEMVER.fullmatch(version):
        raise RuntimeError("a ultima release nao tem versao no formato x.y.z")
    name = asset_name(version)
    asset = next((a for a in release.get("assets", []) if a.get("name") == name), None)
    if asset is None:
        raise RuntimeError("a release %s nao traz o pacote %s" % (version, name))

    # O sha256 sai das notas da release, onde o workflow escreve "<hash>  <arquivo>".
    # Nao e um canal independente do download, mas pega arquivo truncado ou trocado.
    found = re.search(r"([0-9a-f]{64})\s+%s" % re.escape(name), release.get("body") or "")
    # Duas URLs pro mesmo arquivo: a aberta (repo publico) e a da API, que e a
    # unica que aceita token e portanto a unica que serve num repo privado.
    return {"version": version, "name": name, "url": asset["browser_download_url"],
            "api_url": asset["url"], "size": int(asset.get("size") or 0),
            "sha256": found.group(1) if found else ""}


def download(release, on_progress=None, token="", timeout=TIMEOUT):
    """Baixa o pacote numa pasta temporaria e confere o tamanho e o sha256."""
    folder = tempfile.mkdtemp(prefix="streamdeck-update-")
    target = os.path.join(folder, release["name"])
    digest = hashlib.sha256()
    done = 0
    if token:
        url = release.get("api_url") or release["url"]
        heads = headers(token, "application/octet-stream")
    else:
        url, heads = release["url"], headers()
    try:
        with _get(url, heads, timeout) as response:
            total = int(response.headers.get("Content-Length") or release["size"] or 0)
            with open(target, "wb") as f:
                while True:
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    digest.update(chunk)
                    done += len(chunk)
                    if on_progress:
                        on_progress(done, total)
        if release["size"] and done != release["size"]:
            raise RuntimeError("o download veio incompleto (%d de %d bytes)"
                               % (done, release["size"]))
        if release["sha256"] and digest.hexdigest() != release["sha256"]:
            raise RuntimeError("o arquivo baixado nao confere com o sha256 da release")
    except urllib.error.HTTPError as error:
        shutil.rmtree(folder, ignore_errors=True)
        if error.code in (401, 403, 404) and not token:
            raise RuntimeError("o GitHub recusou o download (%s) - se o repositorio for "
                               "privado, ponha um github_token no settings.json" % error.code)
        raise RuntimeError("o download falhou (o GitHub respondeu %s)" % error.code)
    except OSError as error:
        shutil.rmtree(folder, ignore_errors=True)
        raise RuntimeError("o download falhou (%s)" % error)
    except RuntimeError:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    return target


def _tail(done):
    """Ultima linha util do processo que falhou - vira a mensagem do erro."""
    saida = (done.stderr or "") + (done.stdout or "")
    linhas = [l.strip() for l in saida.splitlines() if l.strip()]
    return linhas[-1][:200] if linhas else ""


def install(package, version, dest_dir=BUNDLE_DIR):
    """Instala o pacote baixado. Devolve o texto que o usuario le no fim."""
    if sys.platform == "win32":
        return _install_windows(package, version)
    return _install_linux(package, version, dest_dir)


def _install_windows(package, version):
    # O instalador do Inno roda em silencio e fecha o deck sozinho pra trocar os
    # arquivos; o /REOPEN=1 faz ele reabrir o programa no fim. Fica destacado
    # deste processo, senao morreria junto quando o deck fechar.
    detached = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    log = os.path.join(tempfile.gettempdir(), "streamdeck-update.log")
    subprocess.Popen([package, "/SILENT", "/SUPPRESSMSGBOXES", "/NOCANCEL", "/NORESTART",
                      "/REOPEN=1", "/LOG=" + log],
                     creationflags=detached, close_fds=True)
    return "Instalando a versao %s - o StreamDeck fecha e volta sozinho." % version


def _extract(tar, folder):
    """Extrai recusando o que escapa da pasta ou nao e arquivo/diretorio comum."""
    filtro = getattr(tarfile, "data_filter", None)
    if filtro is not None:
        return tar.extractall(folder, filter=filtro)
    # Python velho, sem o filtro pronto: confere na mao antes de escrever nada.
    base = os.path.realpath(folder)
    for membro in tar.getmembers():
        destino = os.path.realpath(os.path.join(folder, membro.name))
        if destino != base and not destino.startswith(base + os.sep):
            raise RuntimeError("o pacote tenta escrever fora da pasta: %s" % membro.name)
        if not (membro.isfile() or membro.isdir()):
            raise RuntimeError("o pacote traz um membro estranho: %s" % membro.name)
    tar.extractall(folder)


def _install_linux(package, version, dest_dir):
    folder = os.path.dirname(package)
    with tarfile.open(package) as tar:
        _extract(tar, folder)
    root = os.path.join(folder, "StreamDeck-%s" % version)
    script = os.path.join(root, "install.sh")
    if not os.path.exists(script):
        raise RuntimeError("o pacote baixado nao tem install.sh")

    env = {"STREAMDECK_DIR": dest_dir}
    # O install.sh reinicia o servico do systemd - e o servico e justamente este
    # processo. Sob o systemd o script vai pra uma unidade propria, senao ele
    # morreria no meio junto com o deck.
    if os.environ.get("INVOCATION_ID") and shutil.which("systemd-run"):
        started = subprocess.run(
            ["systemd-run", "--user", "--collect", "--quiet",
             "--unit=streamdeck-update-%d" % time.time(),
             "--setenv=STREAMDECK_DIR=%s" % dest_dir, "sh", script],
            capture_output=True, text=True, timeout=30)
        if started.returncode:
            raise RuntimeError(_tail(started) or "systemd-run nao aceitou o install.sh")
        return "Instalando a versao %s - o servico reinicia em instantes." % version

    done = subprocess.run(["sh", script], cwd=root, timeout=300,
                          env=dict(os.environ, **env), capture_output=True, text=True)
    if done.returncode:
        raise RuntimeError(_tail(done) or "o install.sh falhou")
    return "Versao %s instalada - reinicie o StreamDeck pra usar." % version


def selfcheck():
    assert as_tuple("1.2.10") > as_tuple("1.2.9")
    assert is_newer("1.10.0", "1.9.9")
    assert not is_newer("1.1.0", "1.1.0")
    assert not is_newer("1.0.9", "1.1.0")
    assert asset_name("1.2.3") in ("StreamDeck-Setup-1.2.3.exe",
                                   "StreamDeck-1.2.3-linux.tar.gz")
    print("ok: versao instalada %s, atualizacao pelas releases de %s"
          % (installed_version() or "(codigo-fonte, sem version.txt)", REPO))
