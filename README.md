# NFSe Downloader Final

Aplicação local para:

- consultar documentos da NFS-e Nacional por NSU e certificado A1;
- filtrar notas por data;
- gerar o DANFSe localmente conforme a NT 008 v1.02;
- gerar ZIP com os PDFs;
- copiar automaticamente os PDFs para uma pasta de rede;
- executar diariamente às 03h para o dia anterior.

## Instalação

1. Extraia a pasta em um caminho definitivo.
2. Execute `instalar.bat`.
3. Edite `.env`.
4. Coloque `certificado.pfx` na pasta.
5. Execute `iniciar.bat`.

## Automação

Execute `criar_agendamento_03h.bat` como administrador.

A tarefa chama `executar_automatico.bat` todos os dias às 03h e grava o log em:

`dados\automatico.log`

## Fontes da NT 008

O Windows deve possuir:

- Arial;
- Microsoft Sans Serif.

Para bloquear a geração caso as fontes oficiais não sejam encontradas:

`DANFSE_STRICT_FONTS=1`

## Logo

O arquivo `logo-nfs-e-horizontal.png` deve permanecer na mesma pasta do programa.

## Observações de validação

A geração segue as dimensões, fontes mínimas, QR Code, margens, bordas e organização
do Anexo I da NT 008 v1.02. A conformidade dos dados depende das tags presentes no XML.
Campos inexistentes não são inventados.

Antes de produção, valide também XMLs de:

- NFS-e cancelada;
- NFS-e substituída;
- NFS-e sem tomador;
- NFS-e com intermediário;
- NFS-e com IBS/CBS.
