# StreamDeck de bolso

Controla os aplicativos do computador pelo navegador do celular, na mesma rede local.
Funciona no Windows e no Linux com KDE Plasma 6.

## Instalar no Windows

Baixe o `StreamDeck-Setup-<versao>.exe` na [última versão](https://github.com/omarcosvitor/StreamDeack/releases/latest) e execute. O instalador não exige Python nem permissões de administrador. O aplicativo fica na bandeja do Windows, sem terminal aberto. Clique com o botão direito no ícone para ver ou copiar o endereço do celular, abrir a interface no computador ou encerrar o aplicativo.

Os favoritos ficam em `%LOCALAPPDATA%\StreamDeck\apps.json` e são preservados quando o programa é atualizado ou removido.

## Instalar no Linux

Requer KDE Plasma 6 (as janelas são controladas por um script do KWin) e o
`python3-gobject`, que já vem instalado em qualquer Plasma. Sem instalador:

```bash
git clone https://github.com/omarcosvitor/StreamDeack.git ~/.local/share/StreamDeck
python3 ~/.local/share/StreamDeck/deck.py serve
```

Para deixar rodando em segundo plano, um serviço de usuário do systemd:

```bash
systemd-run --user --unit=streamdeck --description='StreamDeck de bolso' \
  python3 ~/.local/share/StreamDeck/deck.py serve
systemctl --user enable --now streamdeck   # opcional: subir junto com a sessão
```

Libere a porta no firewall na primeira vez:
`sudo firewall-cmd --add-port=8765/tcp --permanent && sudo firewall-cmd --reload`

Os favoritos ficam em `apps.json`, ao lado do `deck.py`. O `path` de cada
favorito é o caminho de um arquivo `.desktop`, de uma pasta ou uma URL. O painel
de baixo lista os aplicativos instalados (o Linux não tem o equivalente da lista
de mais usados do Windows) — toque longo promove qualquer um a favorito.

## Atalhos de teclado

Um favorito pode ser um atalho em vez de um aplicativo: o toque manda as teclas
para a janela que estiver em foco no PC (mutar a chamada, pausar o vídeo, `Alt+F4`).

Na página de favoritos, a tecla `+` no fim da lista abre o formulário: marca os
modificadores, escreve o nome da tecla e salva. Toque longo remove, igual a
qualquer favorito.

Dá para escrever direto no `apps.json` também — em vez de `path`, use `keys`:

```json
[
  { "name": "Mutar microfone", "keys": "ctrl+shift+m" },
  { "name": "Volume +",        "keys": "volumeup" },
  { "name": "Fechar",          "keys": "alt+f4" }
]
```

Modificadores: `ctrl`, `shift`, `alt`, `win`. Teclas: `a`-`z`, `0`-`9`, `f1`-`f24`,
`enter`, `esc`, `tab`, `space`, `backspace`, `delete`, `insert`, `home`, `end`,
`pageup`, `pagedown`, `up`, `down`, `left`, `right`, `printscreen`, `menu`,
`capslock`, `volumeup`, `volumedown`, `mute`, `playpause`, `next`, `prev`, `stop`
e a pontuação (`comma`, `period`, `slash`, `backslash`, `semicolon`, `apostrophe`,
`grave`, `bracketleft`, `bracketright`, `minus`, `equal`). Os nomes valem nos dois
sistemas — o mesmo `apps.json` roda no Windows e no Linux.

No Windows funciona sem instalar nada. No Linux o KWin não injeta tecla, então
precisa de uma destas (o deck usa a primeira que encontrar, na ordem certa para a
sessão):

```bash
sudo dnf install ydotool && systemctl --user enable --now ydotool   # Wayland, alcança tudo
sudo dnf install wtype                                             # Wayland, teclado virtual do compositor
sudo dnf install xdotool                                           # X11 (no Wayland só alcança apps XWayland)
```

## Várias telas

Com mais de um monitor, a tecla `TELA` aparece no fim dos favoritos: ela mostra o
número da tela que o deck comanda e abre a lista para trocar. Os aplicativos
lançados ou focados pelo celular vão para essa tela e são maximizados nela. Em
`Automática` o deck não interfere — a janela abre onde o sistema quiser.

A escolha fica em `settings.json`, ao lado do `apps.json` (`{"screen": "DP-2"}`).
O `id` é o nome do dispositivo: `\\.\DISPLAY2` no Windows, o nome do output
(`DP-2`, `HDMI-A-1`) no Linux. Se o monitor escolhido for desconectado, o deck
volta sozinho para o comportamento automático.

## Desenvolvimento

```
python deck.py          bandeja (Windows; no Linux exige pystray, senão cai pro terminal)
python deck.py serve    terminal
python deck.py check    autoteste
```

`deck.py` é comum aos dois sistemas; `deckwin.py` e `decklinux.py` implementam
listar/focar/lançar janelas, extrair ícones e a lista de recentes em cada um.

## Gerar o instalador

Requer Python 3.12+ e Inno Setup 6. O script cria um ambiente virtual e instala as dependências de build automaticamente:

```powershell
winget install JRSoftware.InnoSetup
.\build.ps1 -Version 1.1.0
```

O instalador é criado em `release\StreamDeck-Setup-1.1.0.exe`.
