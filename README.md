# StreamDeck de bolso

Controla os aplicativos do Windows pelo navegador do celular, na mesma rede local.

## Instalar

Baixe o `StreamDeck-Setup-<versao>.exe` na [última versão](https://github.com/omarcosvitor/StreamDeack/releases/latest) e execute. O instalador não exige Python nem permissões de administrador. O aplicativo fica na bandeja do Windows, sem terminal aberto. Clique com o botão direito no ícone para ver ou copiar o endereço do celular, abrir a interface no computador ou encerrar o aplicativo.

Os favoritos ficam em `%LOCALAPPDATA%\StreamDeck\apps.json` e são preservados quando o programa é atualizado ou removido.

## Desenvolvimento

```powershell
python deck.py
python deck.py serve
python deck.py check
```

## Gerar o instalador

Requer Python 3.12+ e Inno Setup 6. O script cria um ambiente virtual e instala as dependências de build automaticamente:

```powershell
winget install JRSoftware.InnoSetup
.\build.ps1 -Version 1.1.0
```

O instalador é criado em `release\StreamDeck-Setup-1.1.0.exe`.
