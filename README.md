# CubeMaster PDF Downloader

Serviço FastAPI que baixa os relatórios em PDF gerados pelo CubeMaster e os grava
na pasta de anexos de clientes do PeopleSoft.

A cada cálculo bem-sucedido, a API do CubeMaster retorna um bloco `reportLinks.pdfLinks`
com URLs de relatório. Este serviço recebe as 3 URLs desejadas + o `title`, baixa cada
PDF, renomeia conforme o padrão acordado e salva na pasta compartilhada.

---

## Índice

- [Arquitetura](#arquitetura)
- [O que baixa (e o que ignora)](#o-que-baixa-e-o-que-ignora)
- [Padrão de nomenclatura](#padrão-de-nomenclatura)
- [Estrutura do projeto](#estrutura-do-projeto)
- [Configuração (variáveis de ambiente)](#configuração-variáveis-de-ambiente)
- [Como rodar](#como-rodar)
- [Contrato da API](#contrato-da-api)
- [Integração com o Micro Integrator](#integração-com-o-micro-integrator)
- [Pontos a confirmar com infra / time](#pontos-a-confirmar-com-infra--time)

---

## Arquitetura

```
PeopleSoft ──► LoadsProxy (WSO2 MI) ──► CubeMaster API
                    │                        │
                    │   resposta com         │
                    │   reportLinks.pdfLinks │
                    ▼                        
             PdfReportProxy (WSO2 MI) ──► cubemaster_pdf (FastAPI/Docker)
                                                 │
                                                 ▼
                                    pasta compartilhada do PeopleSoft
                                    (mobilityFiles/clientes)
```

O serviço roda como container Docker no host de containers (padrão `uvicorn app:app`,
igual aos demais serviços já existentes). O Micro Integrator atua como fachada: o
PeopleSoft (ou o próprio proxy) chama um endpoint MI estável, que encaminha para este
container. Assim o PeopleSoft não precisa conhecer IP/porta do Docker.

> **Quem dispara o download?** Existem dois desenhos possíveis. Ver
> [Integração com o Micro Integrator](#integração-com-o-micro-integrator).

---

## O que baixa (e o que ignora)

O bloco `reportLinks` do CubeMaster tem **dois níveis** com nomes repetidos:

```json
"reportLinks": {
    "pdfLinks": {                       // <-- É DAQUI: versão PDF (URL com &to=PDF&)
        "loadSummary": "...&to=PDF&r=0&",
        "solutions": "...&to=PDF&r=1&",
        "loadingInstruction": "...&to=PDF&r=2&",
        "loadingDiagram": "...&to=PDF&r=3&",
        "loadingRequest": "...&to=PDF&r=4&",
        "placements": "...&to=PDF&r=5&"
    },
    "loadSummary": "...&r=0&",           // <-- versão WEB/HTML (ignorar)
    "loadingInstruction": "...&r=2&",
    "loadingDiagram": "...&r=3&"
}
```

Este serviço baixa **somente 3 links, os que estão DENTRO de `pdfLinks`**:

| Chave (dentro de `pdfLinks`) | Baixa? |
| ---------------------------- | ------ |
| `loadSummary`                | ✅ sim |
| `loadingInstruction`         | ✅ sim |
| `loadingDiagram`             | ✅ sim |
| `solutions`                  | ❌ não |
| `loadingRequest`             | ❌ não |
| `placements`                 | ❌ não |

Os links **de fora** de `pdfLinks` (versão HTML) nunca são usados.

> **`pdfLinks: null`** — quando o cálculo falha (`CargoIsTooSmall`, `CargoIsTooBig`,
> etc.), o CubeMaster não gera relatórios e `pdfLinks` vem `null`. Nesse caso o serviço
> responde `200` sem baixar nada (não é erro). Quem dispara deve tratar esse cenário
> como normal.
>
> **Atenção:** o campo `calculationError` da resposta **não é confiável** para decidir
> isso — em testes reais ele veio preenchido (`"CargoIsTooSmall"`) mesmo em um cálculo
> com `status: "succeed"` e PDFs gerados normalmente. Use sempre `pdfLinks == null`
> como critério, nunca `calculationError`/`status`.

> **`openlink.asp?...&to=PDF&r=N&` não devolve o PDF direto.** Testado com link real:
> essa URL devolve uma página HTML "viewer" (dhtmlx) que carrega o PDF de verdade via
> JavaScript, num padrão como:
> ```html
> attachURL("HTTPS://quality.cubemaster.net:443/reportTemp/LoadSummary_usuario@dominio.com.PDF")
> ```
> O serviço já trata isso automaticamente: se a primeira resposta não for PDF, extrai
> a URL de dentro de `attachURL(...)` e baixa dali. Confirmado que os links **abrem sem
> autenticação** (não foi preciso `CUBEMASTER_TOKEN`).

---

## Padrão de nomenclatura

`<prefixo>_<title>.pdf`, onde o `title` vem do JSON de entrada
(ex.: `AJV4669166-2026-07-01-13.25.51.000000`).

| Origem (`pdfLinks`) | Prefixo             | Exemplo de nome final                                   |
| ------------------- | ------------------- | ------------------------------------------------------- |
| `loadSummary`       | `resumen_carga`     | `resumen_carga_AJV4669166-2026-07-01-13.25.51.000000.pdf` |
| `loadingInstruction`| `instrucciones_carga`| `instrucciones_carga_AJV4669166-...pdf`                |
| `loadingDiagram`    | `diagrama_carga`    | `diagrama_carga_AJV4669166-...pdf`                       |

O `title` é higienizado (removidos `/ \ : * ? " < > |` e `..`) antes de virar nome de
arquivo, para evitar path traversal.

---

## Estrutura do projeto

```
cubemaster_pdf_service/
├── app/
│   ├── __init__.py
│   └── main.py            # aplicacao FastAPI (endpoints + download)
├── requirements.txt       # dependencias
├── Dockerfile             # imagem (uvicorn app.main:app)
├── docker-compose.yml     # servico + volume da pasta destino
├── .dockerignore
└── README.md
```

---

## Configuração (variáveis de ambiente)

| Variável           | Padrão          | Descrição |
| ------------------ | --------------- | --------- |
| `DEST_DIR`         | `/data/clientes`| Pasta de destino **dentro** do container. O volume no compose aponta a pasta real do host para cá. |
| `CUBEMASTER_TOKEN` | *(vazio)*       | Preencher **apenas** se os links `openlink.asp` exigirem autenticação para baixar. Se preenchido, é enviado no header `TokenId` do download. |
| `DOWNLOAD_TIMEOUT` | `60`            | Timeout (segundos) por PDF. |

---

## Como rodar

### Docker Compose (recomendado)

1. Ajuste o **volume** no `docker-compose.yml` para o caminho real da pasta de clientes
   no host (ver [pontos a confirmar](#pontos-a-confirmar-com-infra--time)).
2. Suba:

```bash
docker compose up -d --build
```

3. Verifique:

```bash
curl http://localhost:9097/health
# {"status":"ok","dest_dir":"/data/clientes"}
```

### Local (desenvolvimento, sem Docker)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
DEST_DIR=./out uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
```

Docs interativas em `http://localhost:8080/docs`.

---

## Contrato da API

### `GET /health`

Healthcheck. Retorna `{"status": "ok", "dest_dir": "..."}`.

### `POST /reports/download`

**Request** (`application/json`): a resposta **crua** da API do CubeMaster, repassada
como veio — o proxy WSO2 não precisa extrair nada, só encaminhar o JSON inteiro.
O serviço lê apenas `document.title` e `reportLinks.pdfLinks.{loadSummary,
loadingInstruction, loadingDiagram}`; qualquer outro campo (`filledContainers`,
`shipping`, o `loadSummary` do nível raiz, `solutions`, `loadingRequest`,
`placements`, etc.) é ignorado.

```json
{
  "status": "ok",
  "document": {
    "title": "AJV4669166-2026-07-01-13.25.51.000000"
  },
  "reportLinks": {
    "pdfLinks": {
      "loadSummary": "https://quality.cubemaster.net/source/report/openlink.asp?id=...&to=PDF&r=0&",
      "solutions": "https://quality.cubemaster.net/source/report/openlink.asp?id=...&to=PDF&r=1&",
      "loadingInstruction": "https://quality.cubemaster.net/source/report/openlink.asp?id=...&to=PDF&r=2&",
      "loadingDiagram": "https://quality.cubemaster.net/source/report/openlink.asp?id=...&to=PDF&r=3&",
      "loadingRequest": "https://quality.cubemaster.net/source/report/openlink.asp?id=...&to=PDF&r=4&",
      "placements": "https://quality.cubemaster.net/source/report/openlink.asp?id=...&to=PDF&r=5&"
    }
  }
}
```

Quando o cálculo falha (`CargoIsTooSmall`, `CargoIsTooBig` etc.), `reportLinks.pdfLinks`
vem `null` — o serviço responde `200` sem baixar nada, sem precisar de tratamento
especial de quem chama.

**Response `200`:**

```json
{
  "status": "OK",
  "salvos": [
    "resumen_carga_AJV4669166-2026-07-01-13.25.51.000000.pdf",
    "instrucciones_carga_AJV4669166-2026-07-01-13.25.51.000000.pdf",
    "diagrama_carga_AJV4669166-2026-07-01-13.25.51.000000.pdf"
  ],
  "erros": [],
  "mensagem": null
}
```

**Response `200` (sem links / pdfLinks null):**

```json
{ "status": "SEM_PDF", "salvos": [], "erros": [], "mensagem": "Sem pdfLinks - nada a baixar" }
```

`status`: `OK` (todos salvos), `PARCIAL` (algum falhou, ver `erros`) ou `SEM_PDF`.
O LoadsProxy registra a resposta do downloader no log (etapa `[4]`).

**Response `422`** — o corpo não é uma resposta de cálculo (ex.: erro 400 do CubeMaster).
O motivo é registrado no log do container.
**Response `502`** (todos os downloads falharam) — corpo traz `salvos` e `erros` por arquivo.
**Response `500`** — pasta de destino inacessível (problema de volume/permissão).

### Teste rápido com curl

```bash
curl -X POST http://localhost:9097/reports/download \
  -H "Content-Type: application/json" \
  -d '{
    "document": { "title": "TESTE-001" },
    "reportLinks": {
      "pdfLinks": {
        "loadSummary": "COLE_AQUI_UMA_URL_REAL_DE_pdfLinks",
        "loadingInstruction": "COLE_AQUI",
        "loadingDiagram": "COLE_AQUI"
      }
    }
  }'
```

> Antes de integrar, teste **uma** URL de `pdfLinks` no navegador ou com
> `curl -L -o teste.pdf "URL"`. Se abrir o PDF sem pedir login, o serviço funciona
> como está. Se pedir autenticação ou retornar HTML, preencha `CUBEMASTER_TOKEN`.

---

## Integração com o Micro Integrator

O PeopleSoft chama um proxy MI estável, que encaminha para este container.
Exemplo de proxy fachada:

```xml
<proxy name="PdfReportProxy" transports="http https" startOnLoad="true"
       xmlns="http://ws.apache.org/ns/synapse">
    <target>
        <inSequence>
            <property name="messageType" value="application/json" scope="axis2"/>
            <property name="ContentType" value="application/json" scope="axis2"/>
            <log level="custom">
                <property name="ETAPA" value="[PDF] Encaminhando para downloader"/>
                <property name="BODY" expression="$body"/>
            </log>
            <call>
                <endpoint>
                    <http method="POST" uri-template="http://HOST_DOCKER:9097/reports/download"/>
                </endpoint>
            </call>
            <respond/>
        </inSequence>
        <faultSequence>
            <log level="custom">
                <property name="ETAPA" value="[PDF][ERRO]"/>
                <property name="ERRO" expression="$ctx:ERROR_MESSAGE"/>
            </log>
            <respond/>
        </faultSequence>
    </target>
</proxy>
```

### Gatilho decidido: LoadsProxy agrega tudo

O `LoadsProxy` (proxy WSO2 que já chama a API do CubeMaster) encaminha a resposta
**crua e completa** do CubeMaster para este serviço, sem nenhuma extração prévia —
o serviço já sabe ler `document.title` e `reportLinks.pdfLinks` de dentro do JSON
inteiro e ignora o resto. Não é preciso checar `pdfLinks != null` antes de chamar:
se vier `null` (cálculo falhou: `CargoIsTooSmall`, `CargoIsTooBig` etc.), o serviço
responde `200` sem baixar nada.

O `LoadsProxy` só chama este serviço quando o CubeMaster responde **2xx**. Se o
CubeMaster recusar a carga (ex.: `400` por campo inválido), o downloader não é
chamado e o PeopleSoft recebe o status e o corpo de erro originais do CubeMaster.
O status HTTP devolvido ao PeopleSoft é sempre o do CubeMaster, nunca o deste serviço.

---

## Pontos a confirmar com infra / time

1. ~~**Volume da pasta de destino**~~ — **confirmado**: `/mnt/psappt1/mobilityFiles/clientes`
   (compartilhamento `//psappt1860.ajover.com/MobilityFiles`, montado via CIFS no host de
   containers). Ajustado no `docker-compose.yml` e testado (escrita/leitura ok).

2. ~~**Autenticação dos links**~~ — **confirmado**: os links `openlink.asp?...&to=PDF`
   abrem sem login. Testado ponta a ponta com um `id` real (`b2078a34-d7de-4931-88fb-adf22ceacd31`);
   os 3 PDFs foram baixados e gravados com sucesso, sem precisar de `CUBEMASTER_TOKEN`.

3. ~~**Gatilho**~~ — **decidido**: desenho B, o `LoadsProxy` agrega tudo e encaminha a
   resposta crua do CubeMaster. Ver seção acima.

4. **Cálculo sem PDF** — quando o cálculo falha e não há PDFs, o pedido fica sem anexos.
   Confirmar se isso é aceitável ou se o negócio precisa ser avisado.

5. **Porta do host** — `9097` é exemplo; conferir no `docker ps` uma porta livre.
