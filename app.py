from __future__ import annotations

import base64
import gzip
import io
import json
import os
import re
import sqlite3
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from dotenv import load_dotenv
from flask import (
    Flask,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    send_from_directory,
    url_for,
)
from requests import Response
from requests_pkcs12 import get as pkcs12_get

load_dotenv()

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", APP_DIR / "dados")).resolve()
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", DATA_DIR / "downloads")).resolve()
DB_PATH = Path(os.getenv("DB_PATH", DATA_DIR / "nfse.db")).resolve()

API_BASE_URL = os.getenv(
    "API_BASE_URL",
    "https://adn.nfse.gov.br/contribuintes",
).rstrip("/")

DFE_ENDPOINT_TEMPLATE = os.getenv("DFE_ENDPOINT_TEMPLATE", "/DFe/{nsu}")

DOCUMENTO_PARAM_NAME = os.getenv("DOCUMENTO_PARAM_NAME", "cpf")
DOCUMENTO_CONSULTA = re.sub(r"\D", "", os.getenv("CPF_CONSULTA", ""))

# Endpoint oficial do DANFSe:
# GET https://adn.nfse.gov.br/danfse/{chaveAcesso}
DANFSE_BASE_URL = os.getenv(
    "DANFSE_BASE_URL",
    "https://adn.nfse.gov.br",
).rstrip("/")
DANFSE_ENDPOINT_TEMPLATE = os.getenv(
    "DANFSE_ENDPOINT_TEMPLATE",
    "/danfse/{chave}",
)

PFX_PATH = Path(os.getenv("PFX_PATH", APP_DIR / "certificado.pfx")).resolve()
PFX_PASSWORD = os.getenv("PFX_PASSWORD", "")

REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "60"))
DEFAULT_BATCH_LIMIT = int(os.getenv("DEFAULT_BATCH_LIMIT", "500"))
REQUEST_INTERVAL_SECONDS = float(os.getenv("REQUEST_INTERVAL_SECONDS", "0.35"))
PDF_INTERVAL_SECONDS = float(os.getenv("PDF_INTERVAL_SECONDS", "0.35"))
MAX_RETRIES_429 = int(os.getenv("MAX_RETRIES_429", "2"))

DATA_DIR.mkdir(parents=True, exist_ok=True)
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "nfse-local")


# ---------------------------------------------------------------------------
# Banco de dados
# ---------------------------------------------------------------------------

def db() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def table_columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {
        row["name"]
        for row in con.execute(f"PRAGMA table_info({table})").fetchall()
    }


def add_column_if_missing(
    con: sqlite3.Connection,
    table: str,
    column: str,
    definition: str,
) -> None:
    if column not in table_columns(con, table):
        con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_db() -> None:
    """
    Cria as tabelas e atualiza automaticamente o banco antigo.
    Não é necessário apagar dados/nfse.db.
    """
    with db() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS configuracao (
                chave TEXT PRIMARY KEY,
                valor TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS documentos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nsu INTEGER NOT NULL,
                nome_arquivo TEXT NOT NULL,
                caminho TEXT NOT NULL UNIQUE,
                content_type TEXT,
                tamanho INTEGER NOT NULL DEFAULT 0,
                criado_em TEXT NOT NULL,
                resposta_http INTEGER,
                chave_acesso TEXT,
                data_emissao TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_documentos_nsu
            ON documentos(nsu);

            CREATE INDEX IF NOT EXISTS idx_documentos_data
            ON documentos(data_emissao);

            CREATE TABLE IF NOT EXISTS execucoes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                inicio TEXT NOT NULL,
                fim TEXT,
                nsu_inicial INTEGER NOT NULL,
                nsu_final INTEGER,
                consultados INTEGER NOT NULL DEFAULT 0,
                salvos INTEGER NOT NULL DEFAULT 0,
                vazios INTEGER NOT NULL DEFAULT 0,
                erros INTEGER NOT NULL DEFAULT 0,
                mensagem TEXT
            );
            """
        )

        # Migração do banco criado pela versão anterior.
        add_column_if_missing(con, "documentos", "numero_nfse", "TEXT")
        add_column_if_missing(con, "documentos", "tipo_documento", "TEXT")
        add_column_if_missing(con, "documentos", "caminho_pdf", "TEXT")
        add_column_if_missing(con, "documentos", "erro_pdf", "TEXT")

        con.execute(
            """
            INSERT OR IGNORE INTO configuracao(chave, valor)
            VALUES('ultimo_nsu', '0')
            """
        )


def get_last_nsu() -> int:
    with db() as con:
        row = con.execute(
            "SELECT valor FROM configuracao WHERE chave='ultimo_nsu'"
        ).fetchone()
        return int(row["valor"]) if row else 0


def set_last_nsu(nsu: int) -> None:
    with db() as con:
        con.execute(
            """
            INSERT INTO configuracao(chave, valor)
            VALUES('ultimo_nsu', ?)
            ON CONFLICT(chave) DO UPDATE SET valor=excluded.valor
            """,
            (str(nsu),),
        )


# ---------------------------------------------------------------------------
# Requisições
# ---------------------------------------------------------------------------

def api_url(nsu: int) -> str:
    endpoint = DFE_ENDPOINT_TEMPLATE.format(nsu=nsu)
    if not endpoint.startswith("/"):
        endpoint = "/" + endpoint
    return API_BASE_URL + endpoint


def danfse_url(chave: str) -> str:
    endpoint = DANFSE_ENDPOINT_TEMPLATE.format(chave=chave)
    if not endpoint.startswith("/"):
        endpoint = "/" + endpoint
    return DANFSE_BASE_URL + endpoint


def certificate_get(
    url: str,
    *,
    params: dict[str, str] | None = None,
    accept: str = "*/*",
) -> Response:
    if not PFX_PATH.exists():
        raise FileNotFoundError(f"Certificado não encontrado em: {PFX_PATH}")

    return pkcs12_get(
        url,
        pkcs12_filename=str(PFX_PATH),
        pkcs12_password=PFX_PASSWORD,
        params=params or {},
        timeout=REQUEST_TIMEOUT,
        headers={
            "Accept": accept,
            "User-Agent": "NFSe-Downloader-Local/2.0",
        },
    )


def request_with_429_retry(
    url: str,
    *,
    params: dict[str, str] | None = None,
    accept: str = "*/*",
) -> Response:
    response: Response | None = None

    for attempt in range(MAX_RETRIES_429 + 1):
        response = certificate_get(url, params=params, accept=accept)

        if response.status_code != 429:
            return response

        if attempt >= MAX_RETRIES_429:
            return response

        retry_after = response.headers.get("Retry-After")
        try:
            wait_seconds = max(float(retry_after or 15), 1)
        except ValueError:
            wait_seconds = 15

        time.sleep(wait_seconds)

    assert response is not None
    return response


def request_dfe(nsu: int) -> Response:
    params: dict[str, str] = {}

    if DOCUMENTO_CONSULTA and DOCUMENTO_PARAM_NAME:
        params[DOCUMENTO_PARAM_NAME] = DOCUMENTO_CONSULTA

    return request_with_429_retry(
        api_url(nsu),
        params=params,
        accept="application/json, application/xml, text/xml, */*",
    )


def request_danfse(chave: str) -> Response:
    return request_with_429_retry(
        danfse_url(chave),
        accept="application/pdf, application/json, */*",
    )


# ---------------------------------------------------------------------------
# XML e metadados
# ---------------------------------------------------------------------------

def safe_name(name: str) -> str:
    name = Path(name).name
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    return name[:180] or "documento"


def xml_local_name(tag: str) -> str:
    return tag.split("}", 1)[-1].split(":")[-1]


def first_xml_text(root: ET.Element, names: set[str]) -> str | None:
    normalized = {name.lower() for name in names}

    for element in root.iter():
        if xml_local_name(element.tag).lower() in normalized:
            text = (element.text or "").strip()
            if text:
                return text

    return None


def normalize_date(value: str | None) -> str | None:
    if not value:
        return None

    value = value.strip()

    # Formatos ISO, por exemplo 2026-07-23T10:20:30-03:00.
    match = re.match(r"^(\d{4}-\d{2}-\d{2})", value)
    if match:
        return match.group(1)

    # Formato brasileiro.
    match = re.match(r"^(\d{2})/(\d{2})/(\d{4})", value)
    if match:
        return f"{match.group(3)}-{match.group(2)}-{match.group(1)}"

    return None


def infer_xml_metadata(
    raw: bytes,
    fallback_chave: str | None = None,
) -> dict[str, str | None]:
    result = {
        "chave": fallback_chave,
        "data_emissao": None,
        "numero": None,
    }

    try:
        root = ET.fromstring(raw)

        chave = first_xml_text(
            root,
            {"chNFSe", "chaveAcesso", "ChaveAcesso"},
        )
        numero = first_xml_text(
            root,
            {"nNFSe", "numeroNFSe", "numero", "nDPS"},
        )
        data_bruta = first_xml_text(
            root,
            {"dhEmi", "dEmi", "dataEmissao", "dhProc"},
        )

        # Algumas NFS-e trazem a chave no atributo Id.
        if not chave:
            for element in root.iter():
                for attribute_name, attribute_value in element.attrib.items():
                    if xml_local_name(attribute_name).lower() == "id":
                        match = re.search(r"(\d{40,60})", attribute_value)
                        if match:
                            chave = match.group(1)
                            break
                if chave:
                    break

        result["chave"] = chave or fallback_chave
        result["numero"] = numero
        result["data_emissao"] = normalize_date(data_bruta)

    except ET.ParseError:
        # Fallback por expressões regulares.
        text = raw.decode("utf-8", errors="ignore")

        chave_match = re.search(
            r"<(?:\w+:)?chNFSe>([^<]+)</",
            text,
            re.I,
        )
        numero_match = re.search(
            r"<(?:\w+:)?nNFSe>([^<]+)</",
            text,
            re.I,
        )
        data_match = re.search(
            r"<(?:\w+:)?(?:dhEmi|dEmi|dataEmissao)>([^<]+)</",
            text,
            re.I,
        )

        result["chave"] = (
            chave_match.group(1).strip()
            if chave_match
            else fallback_chave
        )
        result["numero"] = (
            numero_match.group(1).strip()
            if numero_match
            else None
        )
        result["data_emissao"] = normalize_date(
            data_match.group(1) if data_match else None
        )

    return result


def decode_arquivo_xml(value: str) -> bytes:
    """
    Formato retornado pelo ADN:
    Base64 -> GZIP -> XML.
    """
    compacted = base64.b64decode(re.sub(r"\s+", "", value))

    try:
        return gzip.decompress(compacted)
    except gzip.BadGzipFile:
        return compacted


def extract_documents(response: Response) -> tuple[list[dict[str, Any]], str]:
    """
    Interpreta a resposta observada na API:

    {
      "StatusProcessamento": "DOCUMENTOS_LOCALIZADOS",
      "LoteDFe": [{
        "NSU": 2,
        "ChaveAcesso": "...",
        "TipoDocumento": "NFSE",
        "ArquivoXml": "H4sIA..."
      }]
    }
    """
    try:
        data = response.json()
    except ValueError as exc:
        raise ValueError(
            f"A API não retornou JSON válido: {response.text[:500]}"
        ) from exc

    status = str(data.get("StatusProcessamento", "")).upper()
    lote = data.get("LoteDFe") or []

    documents: list[dict[str, Any]] = []

    for item in lote:
        arquivo_xml = item.get("ArquivoXml")
        if not arquivo_xml:
            continue

        returned_nsu = int(item.get("NSU") or 0)
        chave = str(item.get("ChaveAcesso") or "").strip() or None
        tipo = str(item.get("TipoDocumento") or "DFE").strip().upper()

        raw = decode_arquivo_xml(arquivo_xml)
        metadata = infer_xml_metadata(raw, fallback_chave=chave)

        documents.append(
            {
                "nsu": returned_nsu,
                "chave": metadata["chave"],
                "tipo": tipo,
                "numero": metadata["numero"],
                "data_emissao": metadata["data_emissao"],
                "xml": raw,
            }
        )

    return documents, status


# ---------------------------------------------------------------------------
# Salvamento
# ---------------------------------------------------------------------------

def document_folder(data_emissao: str | None) -> Path:
    if data_emissao and re.fullmatch(r"\d{4}-\d{2}-\d{2}", data_emissao):
        year, month, day = data_emissao.split("-")
        folder = DOWNLOAD_DIR / year / month / day
    else:
        now = datetime.now()
        folder = (
            DOWNLOAD_DIR
            / "sem_data"
            / now.strftime("%Y")
            / now.strftime("%m")
            / now.strftime("%d")
        )

    folder.mkdir(parents=True, exist_ok=True)
    return folder


def document_filename(
    nsu: int,
    tipo: str,
    numero: str | None,
    chave: str | None,
) -> str:
    if numero:
        base = f"{tipo}_{numero}"
    elif chave:
        base = f"{tipo}_{chave}"
    else:
        base = f"{tipo}_NSU_{nsu}"

    return safe_name(base + ".xml")


def save_document(document: dict[str, Any], http_status: int) -> bool:
    nsu = int(document["nsu"])
    chave = document.get("chave")
    tipo = document.get("tipo") or "DFE"
    numero = document.get("numero")
    data_emissao = document.get("data_emissao")
    raw: bytes = document["xml"]

    folder = document_folder(data_emissao)
    filename = document_filename(nsu, tipo, numero, chave)
    path = folder / filename

    with db() as con:
        if chave:
            duplicate = con.execute(
                """
                SELECT 1
                FROM documentos
                WHERE chave_acesso=? AND tipo_documento=?
                LIMIT 1
                """,
                (chave, tipo),
            ).fetchone()
        else:
            duplicate = con.execute(
                """
                SELECT 1
                FROM documentos
                WHERE nsu=? AND tipo_documento=?
                LIMIT 1
                """,
                (nsu, tipo),
            ).fetchone()

        if duplicate:
            return False

        # Evita sobrescrever arquivo diferente com o mesmo nome.
        if path.exists():
            path = folder / safe_name(f"{path.stem}_NSU_{nsu}{path.suffix}")

        path.write_bytes(raw)

        con.execute(
            """
            INSERT INTO documentos(
                nsu,
                nome_arquivo,
                caminho,
                content_type,
                tamanho,
                criado_em,
                resposta_http,
                chave_acesso,
                data_emissao,
                numero_nfse,
                tipo_documento
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                nsu,
                path.name,
                str(path),
                "application/xml",
                len(raw),
                datetime.now().isoformat(timespec="seconds"),
                http_status,
                chave,
                data_emissao,
                numero,
                tipo,
            ),
        )

    return True


def save_pdf_for_document(row: sqlite3.Row) -> tuple[Path | None, str | None]:
    chave = (row["chave_acesso"] or "").strip()
    if not chave:
        return None, "Documento sem chave de acesso."

    current_pdf = row["caminho_pdf"]
    if current_pdf:
        current_path = Path(current_pdf)
        if current_path.exists():
            return current_path, None

    response = request_danfse(chave)

    if response.status_code == 429:
        return None, "API do DANFSe respondeu HTTP 429."

    if response.status_code != 200:
        return (
            None,
            f"HTTP {response.status_code}: {response.text[:300]}",
        )

    content_type = (response.headers.get("Content-Type") or "").lower()
    content = response.content or b""

    if not (
        content.startswith(b"%PDF")
        or "application/pdf" in content_type
    ):
        return (
            None,
            "A API do DANFSe não retornou um PDF. "
            f"Content-Type: {content_type}; resposta: {response.text[:300]}",
        )

    xml_path = Path(row["caminho"])
    pdf_folder = xml_path.parent
    pdf_name = safe_name(xml_path.stem + ".pdf")
    pdf_path = pdf_folder / pdf_name
    pdf_path.write_bytes(content)

    with db() as con:
        con.execute(
            """
            UPDATE documentos
            SET caminho_pdf=?, erro_pdf=NULL
            WHERE id=?
            """,
            (str(pdf_path), row["id"]),
        )

    return pdf_path, None


# ---------------------------------------------------------------------------
# Sincronização
# ---------------------------------------------------------------------------

def run_download(start_nsu: int, limit: int) -> dict[str, Any]:
    counters: dict[str, Any] = {
        "consultados": 0,
        "salvos": 0,
        "vazios": 0,
        "erros": 0,
        "ultimo_nsu": start_nsu - 1,
    }

    cursor_nsu = start_nsu
    execution_id: int
    message = "Concluído."

    with db() as con:
        cursor = con.execute(
            """
            INSERT INTO execucoes(inicio, nsu_inicial)
            VALUES (?, ?)
            """,
            (
                datetime.now().isoformat(timespec="seconds"),
                start_nsu,
            ),
        )
        execution_id = cursor.lastrowid

    try:
        while counters["consultados"] < limit:
            counters["consultados"] += 1

            try:
                response = request_dfe(cursor_nsu)
            except Exception as exc:
                counters["erros"] += 1
                message = (
                    f"Falha de conexão/certificado no NSU {cursor_nsu}: {exc}"
                )
                break

            if response.status_code in (401, 403):
                counters["erros"] += 1
                message = (
                    "A API recusou o certificado ou a autorização "
                    f"(HTTP {response.status_code}): {response.text[:500]}"
                )
                break

            if response.status_code == 429:
                counters["erros"] += 1
                message = (
                    "A API limitou as consultas (HTTP 429). "
                    "Aguarde o período indicado pelo portal antes de tentar novamente."
                )
                break

            if response.status_code >= 400:
                counters["erros"] += 1
                message = (
                    f"Erro HTTP {response.status_code} no NSU {cursor_nsu}: "
                    f"{response.text[:700]}"
                )
                break

            try:
                documents, status = extract_documents(response)
            except Exception as exc:
                counters["erros"] += 1
                message = (
                    f"Não foi possível interpretar o retorno do NSU "
                    f"{cursor_nsu}: {exc}"
                )
                break

            if not documents:
                counters["vazios"] += 1
                counters["ultimo_nsu"] = max(
                    counters["ultimo_nsu"],
                    cursor_nsu - 1,
                )

                # A documentação orienta parar e aguardar quando não houver
                # mais documentos, em vez de continuar insistindo.
                message = (
                    f"Nenhum novo documento. Status: "
                    f"{status or 'SEM_DOCUMENTOS'}."
                )
                break

            returned_nsus: list[int] = []

            for document in documents:
                returned_nsu = int(document["nsu"])
                returned_nsus.append(returned_nsu)

                if save_document(document, response.status_code):
                    counters["salvos"] += 1

            highest_returned = max(returned_nsus)
            counters["ultimo_nsu"] = max(
                counters["ultimo_nsu"],
                highest_returned,
            )
            set_last_nsu(counters["ultimo_nsu"])

            # O teste mostrou que uma consulta ao NSU 1 pode devolver NSU 2.
            # Por isso, o próximo pedido usa o maior NSU retornado + 1.
            cursor_nsu = highest_returned + 1

            if REQUEST_INTERVAL_SECONDS > 0:
                time.sleep(REQUEST_INTERVAL_SECONDS)

    finally:
        if counters["ultimo_nsu"] >= start_nsu:
            set_last_nsu(counters["ultimo_nsu"])

        with db() as con:
            con.execute(
                """
                UPDATE execucoes
                SET fim=?,
                    nsu_final=?,
                    consultados=?,
                    salvos=?,
                    vazios=?,
                    erros=?,
                    mensagem=?
                WHERE id=?
                """,
                (
                    datetime.now().isoformat(timespec="seconds"),
                    counters["ultimo_nsu"],
                    counters["consultados"],
                    counters["salvos"],
                    counters["vazios"],
                    counters["erros"],
                    message,
                    execution_id,
                ),
            )

    counters["mensagem"] = message
    return counters


# ---------------------------------------------------------------------------
# Filtro e ZIP
# ---------------------------------------------------------------------------

def validate_period(
    data_inicial: str | None,
    data_final: str | None,
) -> tuple[str, str]:
    pattern = r"\d{4}-\d{2}-\d{2}"

    if not data_inicial or not data_final:
        raise ValueError("Informe a data inicial e a data final.")

    if not re.fullmatch(pattern, data_inicial):
        raise ValueError("Data inicial inválida.")

    if not re.fullmatch(pattern, data_final):
        raise ValueError("Data final inválida.")

    if data_inicial > data_final:
        raise ValueError(
            "A data inicial não pode ser posterior à data final."
        )

    return data_inicial, data_final


def find_documents_by_period(
    data_inicial: str,
    data_final: str,
) -> list[sqlite3.Row]:
    with db() as con:
        return con.execute(
            """
            SELECT *
            FROM documentos
            WHERE tipo_documento='NFSE'
              AND data_emissao BETWEEN ? AND ?
            ORDER BY data_emissao, numero_nfse, nsu
            """,
            (data_inicial, data_final),
        ).fetchall()


def build_period_zip(
    data_inicial: str,
    data_final: str,
    include_xml: bool,
    include_pdf: bool,
) -> tuple[io.BytesIO, int, list[str]]:
    rows = find_documents_by_period(data_inicial, data_final)

    if not rows:
        raise ValueError(
            "Nenhuma NFS-e foi localizada no período. "
            "Primeiro sincronize os documentos pela tela principal."
        )

    output = io.BytesIO()
    errors: list[str] = []

    with zipfile.ZipFile(
        output,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
    ) as archive:
        for index, row in enumerate(rows, start=1):
            xml_path = Path(row["caminho"])

            if include_xml and xml_path.exists():
                archive.write(
                    xml_path,
                    arcname=f"XML/{xml_path.name}",
                )

            if include_pdf:
                pdf_path, error = save_pdf_for_document(row)

                if pdf_path and pdf_path.exists():
                    archive.write(
                        pdf_path,
                        arcname=f"PDF/{pdf_path.name}",
                    )
                else:
                    identification = (
                        row["numero_nfse"]
                        or row["chave_acesso"]
                        or f"NSU {row['nsu']}"
                    )
                    errors.append(f"{identification}: {error}")

                    with db() as con:
                        con.execute(
                            """
                            UPDATE documentos
                            SET erro_pdf=?
                            WHERE id=?
                            """,
                            (error, row["id"]),
                        )

                if index < len(rows) and PDF_INTERVAL_SECONDS > 0:
                    time.sleep(PDF_INTERVAL_SECONDS)

        if errors:
            archive.writestr(
                "ERROS_PDF.txt",
                "\n".join(errors),
            )

    output.seek(0)
    return output, len(rows), errors


# ---------------------------------------------------------------------------
# Rotas
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    data_inicial = request.args.get("data_inicial", "")
    data_final = request.args.get("data_final", "")

    with db() as con:
        docs = con.execute(
            """
            SELECT *
            FROM documentos
            ORDER BY id DESC
            LIMIT 100
            """
        ).fetchall()

        executions = con.execute(
            """
            SELECT *
            FROM execucoes
            ORDER BY id DESC
            LIMIT 20
            """
        ).fetchall()

    filtered_docs: list[sqlite3.Row] = []
    if data_inicial and data_final:
        try:
            validate_period(data_inicial, data_final)
            filtered_docs = find_documents_by_period(
                data_inicial,
                data_final,
            )
        except ValueError:
            filtered_docs = []

    return render_template(
        "index.html",
        docs=docs,
        filtered_docs=filtered_docs,
        executions=executions,
        last_nsu=get_last_nsu(),
        default_limit=DEFAULT_BATCH_LIMIT,
        pfx_ok=PFX_PATH.exists(),
        pfx_path=str(PFX_PATH),
        api_base=API_BASE_URL,
        documento=DOCUMENTO_CONSULTA,
        data_inicial=data_inicial,
        data_final=data_final,
    )


@app.post("/baixar")
def baixar():
    try:
        start_nsu = int(
            request.form.get("nsu_inicial")
            or (get_last_nsu() + 1)
        )
        limit = int(
            request.form.get("limite")
            or DEFAULT_BATCH_LIMIT
        )

        if start_nsu < 0:
            raise ValueError("NSU inicial não pode ser negativo.")

        if not 1 <= limit <= 10000:
            raise ValueError(
                "O limite deve ficar entre 1 e 10.000."
            )

        result = run_download(start_nsu, limit)
        category = (
            "success"
            if result["erros"] == 0
            else "warning"
        )

        flash(
            f'{result["mensagem"]} '
            f'Consultas: {result["consultados"]}; '
            f'arquivos novos: {result["salvos"]}; '
            f'vazios: {result["vazios"]}; '
            f'erros: {result["erros"]}.',
            category,
        )

    except Exception as exc:
        flash(str(exc), "danger")

    return redirect(url_for("index"))


@app.get("/filtrar")
def filtrar():
    try:
        data_inicial, data_final = validate_period(
            request.args.get("data_inicial"),
            request.args.get("data_final"),
        )

        return redirect(
            url_for(
                "index",
                data_inicial=data_inicial,
                data_final=data_final,
            )
        )
    except ValueError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("index"))


@app.get("/baixar-periodo")
def baixar_periodo():
    try:
        data_inicial, data_final = validate_period(
            request.args.get("data_inicial"),
            request.args.get("data_final"),
        )

        include_xml = request.args.get("xml", "1") == "1"
        include_pdf = request.args.get("pdf", "1") == "1"

        if not include_xml and not include_pdf:
            raise ValueError(
                "Escolha pelo menos XML ou PDF."
            )

        zip_buffer, total, pdf_errors = build_period_zip(
            data_inicial,
            data_final,
            include_xml=include_xml,
            include_pdf=include_pdf,
        )

        filename = (
            f"NFSe_{data_inicial}_a_{data_final}.zip"
        )

        if pdf_errors:
            flash(
                f"ZIP criado com {total} nota(s), mas "
                f"{len(pdf_errors)} PDF(s) não foram obtidos. "
                "Veja o arquivo ERROS_PDF.txt dentro do ZIP.",
                "warning",
            )

        return send_file(
            zip_buffer,
            mimetype="application/zip",
            as_attachment=True,
            download_name=filename,
        )

    except Exception as exc:
        flash(str(exc), "danger")
        return redirect(url_for("index"))


@app.post("/ajustar-nsu")
def ajustar_nsu():
    try:
        nsu = int(request.form["ultimo_nsu"])

        if nsu < 0:
            raise ValueError

        set_last_nsu(nsu)
        flash(
            f"Último NSU alterado para {nsu}.",
            "success",
        )

    except Exception:
        flash("Informe um NSU válido.", "danger")

    return redirect(url_for("index"))


@app.route("/arquivo/<int:doc_id>")
def arquivo(doc_id: int):
    with db() as con:
        row = con.execute(
            """
            SELECT caminho, nome_arquivo
            FROM documentos
            WHERE id=?
            """,
            (doc_id,),
        ).fetchone()

    if not row:
        return "Arquivo não encontrado.", 404

    path = Path(row["caminho"])

    if not path.exists():
        return "O arquivo não existe mais no disco.", 404

    return send_from_directory(
        path.parent,
        path.name,
        as_attachment=True,
        download_name=row["nome_arquivo"],
    )


@app.route("/pdf/<int:doc_id>")
def pdf(doc_id: int):
    with db() as con:
        row = con.execute(
            """
            SELECT *
            FROM documentos
            WHERE id=?
            """,
            (doc_id,),
        ).fetchone()

    if not row:
        return "Documento não encontrado.", 404

    pdf_path, error = save_pdf_for_document(row)

    if not pdf_path:
        return f"Não foi possível obter o PDF: {error}", 502

    return send_from_directory(
        pdf_path.parent,
        pdf_path.name,
        as_attachment=True,
        download_name=pdf_path.name,
    )


@app.route("/teste")
def teste():
    try:
        nsu = int(
            request.args.get(
                "nsu",
                get_last_nsu() + 1,
            )
        )
        response = request_dfe(nsu)
        preview = response.text[:5000]

        return (
            f"<h2>Teste do NSU {nsu}</h2>"
            f"<p>HTTP: {response.status_code}</p>"
            f"<p>Content-Type: "
            f"{response.headers.get('Content-Type')}</p>"
            f"<pre style='white-space:pre-wrap'>"
            f"{preview}</pre>"
        )

    except Exception as exc:
        return (
            f"<h2>Falha no teste</h2>"
            f"<pre>{exc}</pre>",
            500,
        )


@app.route("/teste-pdf/<chave>")
def teste_pdf(chave: str):
    """
    Teste direto:
    http://127.0.0.1:5000/teste-pdf/CHAVE_DA_NOTA
    """
    try:
        response = request_danfse(chave)

        return (
            f"<h2>Teste DANFSe</h2>"
            f"<p>URL: {danfse_url(chave)}</p>"
            f"<p>HTTP: {response.status_code}</p>"
            f"<p>Content-Type: "
            f"{response.headers.get('Content-Type')}</p>"
            f"<p>Tamanho: {len(response.content)} bytes</p>"
            f"<pre style='white-space:pre-wrap'>"
            f"{response.text[:1000] if not response.content.startswith(b'%PDF') else 'PDF recebido com sucesso.'}"
            f"</pre>"
        )

    except Exception as exc:
        return (
            f"<h2>Falha no teste do DANFSe</h2>"
            f"<pre>{exc}</pre>",
            500,
        )


if __name__ == "__main__":
    init_db()
    app.run(
        host="127.0.0.1",
        port=5000,
        debug=False,
    )
