NFS-e DOWNLOADER LOCAL
======================

1. Instale o Python 3.11 ou superior:
   - Durante a instalação, marque "Add Python to PATH".

2. Execute:
   instalar.bat

3. Renomeie/copie .env.example para .env.
   O instalar.bat já faz isso automaticamente quando necessário.

4. Edite o arquivo .env:
   - PFX_PASSWORD
   - CNPJ_CONSULTA
   - confira PFX_PATH

5. Coloque o certificado A1 na pasta do programa com o nome:
   certificado.pfx

6. Execute:
   iniciar.bat

7. Acesse:
   http://127.0.0.1:5000

IMPORTANTE
----------
A documentação oficial de fevereiro de 2026 confirma o método:
GET /DFe/{NSU}

Ela também informa que existe um parâmetro para indicar um CNPJ de consulta
diferente do certificado, desde que possua o mesmo CNPJ raiz. Contudo, o manual
resumido não informa o nome técnico exato do parâmetro. Este projeto usa "cnpj"
como padrão, mas ele pode ser alterado no .env:

CNPJ_PARAM_NAME=cnpj

Confira o nome atual no Swagger oficial:
https://adn.nfse.gov.br/contribuintes/docs/index.html

Se o teste retornar HTTP 400 indicando parâmetro inválido ou obrigatório,
copie a mensagem exibida pela rota /teste e ajuste:
- API_BASE_URL
- DFE_ENDPOINT_TEMPLATE
- CNPJ_PARAM_NAME

Os documentos ficam em:
dados\downloads\ANO\MES\DIA

O banco local fica em:
dados\nfse.db
