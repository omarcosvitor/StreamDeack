#!/usr/bin/env sh
# Instala o StreamDeck de bolso como servico de usuario do systemd.
#
#   ./install.sh              instala e sobe o servico
#   ./install.sh --uninstall  para e remove o servico (mantem os favoritos)
set -eu

DEST="${STREAMDECK_DIR:-$HOME/.local/share/StreamDeck}"
UNIT="$HOME/.config/systemd/user/streamdeck.service"
ORIGEM="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
PORTA="${DECK_PORT:-8765}"

if [ "${1:-}" = "--uninstall" ]; then
    systemctl --user disable --now streamdeck 2>/dev/null || true
    rm -f "$UNIT"
    systemctl --user daemon-reload
    echo "Servico removido. O programa e os favoritos seguem em $DEST"
    exit 0
fi

command -v python3 >/dev/null || { echo "python3 nao encontrado." >&2; exit 1; }
python3 -c 'import gi' 2>/dev/null || {
    echo "python3-gobject ausente - instale (Fedora: sudo dnf install python3-gobject)." >&2
    exit 1
}

# apps.json e settings.json moram nesta pasta: copia so os arquivos do programa,
# nunca limpa o destino, senao a atualizacao levaria os favoritos junto.
mkdir -p "$DEST" "$(dirname "$UNIT")"
for arquivo in deck.py decklinux.py deckupdate.py index.html install.sh; do
    cp "$ORIGEM/$arquivo" "$DEST/$arquivo"
done
chmod +x "$DEST/install.sh"
# So o pacote publicado traz o version.txt; sem ele o deck sabe que roda do
# codigo-fonte e nao se atualiza sozinho.
if [ -f "$ORIGEM/version.txt" ]; then
    cp "$ORIGEM/version.txt" "$DEST/version.txt"
fi

cat > "$UNIT" <<UNIDADE
[Unit]
Description=StreamDeck de bolso
After=graphical-session.target

[Service]
Environment=DECK_PORT=$PORTA
ExecStart=$(command -v python3) $DEST/deck.py serve
Restart=on-failure

[Install]
WantedBy=default.target
UNIDADE

# enable + restart em vez de "enable --now": numa atualizacao o servico ja esta
# de pe e precisa reiniciar pra carregar o codigo novo.
systemctl --user daemon-reload
systemctl --user enable streamdeck
systemctl --user restart streamdeck

echo "Instalado em $DEST e rodando na porta $PORTA."
systemctl --user --no-pager --lines=5 status streamdeck || true
echo
echo "Libere a porta no firewall na primeira vez:"
echo "  sudo firewall-cmd --add-port=$PORTA/tcp --permanent && sudo firewall-cmd --reload"
