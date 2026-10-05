"""
CubeMaster PDF Downloader Service
----------------------------------
Recebe os links de relatorio (pdfLinks) retornados pela API do CubeMaster,
baixa os 3 PDFs desejados (loadSummary, loadingInstruction, loadingDiagram),
renomeia conforme o padrao acordado e grava na pasta compartilhada do PeopleSoft.

Padrao de nome: <prefixo>_<title>.pdf
    loadSummary        -> resumen_carga_<title>.pdf
    loadingInstruction -> instrucciones_carga_<title>.pdf
    loadingDiagram     -> diagrama_carga_<title>.pdf
"""

import logging
import os
import re
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field

# openlink.asp?...&to=PDF&r=N& nao devolve o PDF direto: devolve uma pagina
# HTML "viewer" que carrega o PDF real via JS, ex.:
#   attachURL("HTTPS://quality.cubemaster.net:443/reportTemp/LoadSummary_x.PDF")
ATTACH_URL_RE = re.compile(r'attachURL\("([^"]+)"\)', re.IGNORECASE)

# ---------------------------------------------------------------------------
# Configuracao
# ---------------------------------------------------------------------------

# Pasta de destino (montada como volume no container - ver docker-compose.yml)
DEST_DIR = os.environ.get("DEST_DIR", "/data/clientes")

# Header opcional caso os links do CubeMaster exijam autenticacao.
# Deixe vazio se os links abrem sem token.
CUBEMASTER_TOKEN = os.environ.get("CUBEMASTER_TOKEN", "").strip()

# Timeout (segundos) para o download de cada PDF
DOWNLOAD_TIMEOUT = int(os.environ.get("DOWNLOAD_TIMEOUT", "60"))

# Mapeia cada chave de pdfLinks ao prefixo do nome de arquivo
PREFIXOS = {
    "loadSummary": "resumen_carga",
    "loadingInstruction": "instrucciones_carga",
    "loadingDiagram": "diagrama_carga",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("cubemaster_pdf")

app = FastAPI(
    title="CubeMaster PDF Downloader",
    description="Baixa relatorios PDF do CubeMaster e grava na pasta de anexos de clientes.",
    version="1.0.0",
)


# ---------------------------------------------------------------------------
# Modelos
# ---------------------------------------------------------------------------

class PdfLinks(BaseModel):
    """Bloco reportLinks.pdfLinks da resposta do CubeMaster. So usamos 3 chaves;
    as demais (solutions, loadingRequest, placements) sao aceitas e ignoradas."""
    model_config = ConfigDict(extra="ignore")

    loadSummary: Optional[str] = None
    loadingInstruction: Optional[str] = None
    loadingDiagram: Optional[str] = None
    solutions: Optional[str] = None
    loadingRequest: Optional[str] = None
    placements: Optional[str] = None


class ReportLinks(BaseModel):
    """Bloco reportLinks da resposta do CubeMaster. pdfLinks vem null quando
    o calculo falha (CargoIsTooSmall, CargoIsTooBig etc.)."""
    model_config = ConfigDict(extra="ignore")

    pdfLinks: Optional[PdfLinks] = None


class Document(BaseModel):
    """Bloco document da resposta do CubeMaster. So o title interessa aqui."""
    model_config = ConfigDict(extra="ignore")

    title: str


class ReportRequest(BaseModel):
    """Corpo esperado na chamada de download: a resposta CRUA da API do
    CubeMaster, repassada como veio (o proxy WSO2 nao precisa extrair nada,
    so encaminhar o JSON). Campos extras (loadSummary do topo, filledContainers,
    shipping etc.) sao ignorados.
    """
    model_config = ConfigDict(extra="ignore")

    document: Document = Field(..., description="Bloco document da resposta do CubeMaster (usa document.title).")
    reportLinks: Optional[ReportLinks] = Field(None, description="Bloco reportLinks da resposta do CubeMaster.")


class DownloadResult(BaseModel):
    # OK = todos salvos | PARCIAL = algum falhou | SEM_PDF = pdfLinks null/vazio.
    status: str
    salvos: list
    erros: list
    mensagem: Optional[str] = None


# ---------------------------------------------------------------------------
# Logica de download
# ---------------------------------------------------------------------------

def _sanitize(title: str) -> str:
    """Remove caracteres perigosos do title antes de usar no nome do arquivo."""
    proibidos = ['/', '\\', '..', '\x00', ':', '*', '?', '"', '<', '>', '|']
    limpo = title.strip()
    for c in proibidos:
        limpo = limpo.replace(c, '_')
    return limpo


def _baixar_pdf(url: str, destino: str) -> None:
    """Baixa uma URL e grava em `destino`. Valida que o conteudo e um PDF.

    O link de reportLinks.pdfLinks (openlink.asp?...&to=PDF&r=N&) nao devolve
    o PDF direto: devolve uma pagina HTML "viewer" que carrega o PDF real via
    JS (attachURL("https://.../reportTemp/Nome_usuario.PDF")). Se a primeira
    resposta nao for PDF, extraimos essa URL do HTML e baixamos ela.
    """
    headers = {}
    if CUBEMASTER_TOKEN:
        # Se os links exigirem o mesmo token da API do CubeMaster
        headers["TokenId"] = CUBEMASTER_TOKEN

    with httpx.Client(timeout=DOWNLOAD_TIMEOUT, follow_redirects=True) as client:
        resp = client.get(url, headers=headers)
        resp.raise_for_status()
        conteudo = resp.content

        if conteudo[:4] != b"%PDF":
            match = ATTACH_URL_RE.search(conteudo.decode("utf-8", errors="replace"))
            if not match:
                preview = conteudo[:120].decode("utf-8", errors="replace")
                raise ValueError(
                    f"conteudo nao e PDF e nao achei attachURL (comeca com: {preview!r})"
                )
            url_real = match.group(1)
            resp = client.get(url_real, headers=headers)
            resp.raise_for_status()
            conteudo = resp.content

            if conteudo[:4] != b"%PDF":
                preview = conteudo[:120].decode("utf-8", errors="replace")
                raise ValueError(
                    f"conteudo do attachURL tambem nao e PDF (comeca com: {preview!r})"
                )

        with open(destino, "wb") as f:
            f.write(conteudo)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.exception_handler(RequestValidationError)
async def log_validation_error(request: Request, exc: RequestValidationError):
    """Registra no log o motivo do 422 (antes so aparecia a linha do uvicorn)."""
    logger.warning("422 em %s: %s", request.url.path, exc.errors())
    return await request_validation_exception_handler(request, exc)


@app.get("/health")
def health():
    """Healthcheck simples."""
    return {"status": "ok", "dest_dir": DEST_DIR}


@app.post("/reports/download", response_model=DownloadResult)
def download_reports(req: ReportRequest):
    """Recebe a resposta crua da API do CubeMaster, baixa os PDFs desejados
    (dentro de reportLinks.pdfLinks) e grava na pasta de destino.

    Retorna a lista de arquivos salvos e eventuais erros por arquivo.
    Se pdfLinks vier null (calculo falhou: CargoIsTooSmall, CargoIsTooBig etc.)
    ou nenhum dos 3 links desejados estiver preenchido, responde 200 sem baixar nada.
    """
    title = _sanitize(req.document.title)

    pdf_links = req.reportLinks.pdfLinks if req.reportLinks else None

    # Monta o dicionario apenas com os links efetivamente informados
    links = {
        "loadSummary": pdf_links.loadSummary if pdf_links else None,
        "loadingInstruction": pdf_links.loadingInstruction if pdf_links else None,
        "loadingDiagram": pdf_links.loadingDiagram if pdf_links else None,
    }
    links = {chave: url for chave, url in links.items() if url}

    if not links:
        logger.info("Nenhum pdfLink informado para title=%s. Nada a baixar.", title)
        return DownloadResult(status="SEM_PDF", salvos=[], erros=[], mensagem="Sem pdfLinks - nada a baixar")

    try:
        os.makedirs(DEST_DIR, exist_ok=True)
    except OSError as e:
        logger.error("Nao foi possivel criar/acessar DEST_DIR=%s: %s", DEST_DIR, e)
        raise HTTPException(status_code=500, detail=f"Pasta de destino inacessivel: {e}")

    salvos, erros = [], []

    for chave, url in links.items():
        nome = f"{PREFIXOS[chave]}_{title}.pdf"
        destino = os.path.join(DEST_DIR, nome)
        try:
            _baixar_pdf(url, destino)
            salvos.append(nome)
            logger.info("PDF salvo: %s", destino)
        except Exception as e:
            erros.append({"arquivo": nome, "url": url, "erro": str(e)})
            logger.error("Falha ao baixar %s (%s): %s", nome, url, e)

    # Se todos falharam, retorna 502 para o chamador saber que nada foi gravado
    if erros and not salvos:
        raise HTTPException(
            status_code=502,
            detail={"salvos": salvos, "erros": erros},
        )

    return DownloadResult(status="PARCIAL" if erros else "OK", salvos=salvos, erros=erros)
