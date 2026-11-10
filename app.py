from __future__ import annotations

import base64
import gzip
import io
import json
import os
import re
import shutil
import sqlite3
import sys
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
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
from requests.exceptions import RequestException
from requests_pkcs12 import get as pkcs12_get

from danfse_generator import GeneratorOptions, MunicipalityInfo, generate_danfse_pdf
from nfse_xml import parse_nfse_file

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

# Geração local do DANFSe, conforme a Nota Técnica nº 008.
MUNICIPALITY_NAME = os.getenv(
    "MUNICIPALITY_NAME",
    "Prefeitura de Joinville",
)
MUNICIPALITY_PHONE = os.getenv(
    "MUNICIPALITY_PHONE",
    "(47) 3431-3233",
)
MUNICIPALITY_EMAIL = os.getenv(
    "MUNICIPALITY_EMAIL",
    "fiscalville@joinville.sc.gov.br",
)

MUNICIPALITY_LOGO_RAW = os.getenv(
    "MUNICIPALITY_LOGO_PATH",
    "",
).strip()
MUNICIPALITY_LOGO_PATH = (
    Path(MUNICIPALITY_LOGO_RAW).expanduser()
    if MUNICIPALITY_LOGO_RAW
    else APP_DIR / "logo-nfs-e-horizontal.png"
)

IBGE_MUNICIPALITIES_FILE_RAW = os.getenv(
    "IBGE_MUNICIPALITIES_FILE",
    "",
).strip()
IBGE_MUNICIPALITIES_FILE = (
    Path(IBGE_MUNICIPALITIES_FILE_RAW).expanduser()
    if IBGE_MUNICIPALITIES_FILE_RAW
    else None
)

MUNICIPALITY_INFO = MunicipalityInfo(
    name=MUNICIPALITY_NAME,
    phone=MUNICIPALITY_PHONE,
    email=MUNICIPALITY_EMAIL,
    logo_path=MUNICIPALITY_LOGO_PATH,
)


DANFSE_OPTIONS = GeneratorOptions(
    include_stub=os.getenv("DANFSE_INCLUDE_STUB", "0") == "1",
    suppress_optional_address_lines=(
        os.getenv("DANFSE_SUPPRESS_OPTIONAL_LINES", "0") == "1"
    ),
    strict_fonts=os.getenv("DANFSE_STRICT_FONTS", "0") == "1",
    strict_logo=True,
    strict_one_page=True,
)

# Municípios mais utilizados pelo cartório. Outros podem ser incluídos
# pelo arquivo JSON configurado em IBGE_MUNICIPALITIES_FILE.
IBGE_MUNICIPALITIES: dict[str, tuple[str, str]] = {
    "4209102": ("Joinville", "SC"),
    "4208450": ("Itapoá", "SC"),
}

PFX_PATH = Path(os.getenv("PFX_PATH", APP_DIR / "certificado.pfx")).resolve()
PFX_PASSWORD = os.getenv("PFX_PASSWORD", "")

REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "60"))
DEFAULT_BATCH_LIMIT = int(os.getenv("DEFAULT_BATCH_LIMIT", "500"))
REQUEST_INTERVAL_SECONDS = float(os.getenv("REQUEST_INTERVAL_SECONDS", "0.35"))
PDF_INTERVAL_SECONDS = float(os.getenv("PDF_INTERVAL_SECONDS", "0"))
PDF_MAX_WORKERS = int(os.getenv("PDF_MAX_WORKERS", "3"))
MAX_RETRIES_429 = int(os.getenv("MAX_RETRIES_429", "3"))
MAX_RETRIES_SERVER = int(os.getenv("MAX_RETRIES_SERVER", "5"))
MAX_RETRIES_CONNECTION = int(os.getenv("MAX_RETRIES_CONNECTION", "4"))
RETRY_SERVER_SECONDS = float(os.getenv("RETRY_SERVER_SECONDS", "3"))
AUTO_DIR = Path(os.getenv("AUTO_DIR", DATA_DIR / "automaticos")).resolve()
JOB_DIR = Path(os.getenv("JOB_DIR", DATA_DIR / "jobs")).resolve()
NETWORK_PDF_DIR_RAW = os.getenv("NETWORK_PDF_DIR", "").strip()
NETWORK_PDF_DIR = Path(NETWORK_PDF_DIR_RAW) if NETWORK_PDF_DIR_RAW else None

DATA_DIR.mkdir(parents=True, exist_ok=True)
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
AUTO_DIR.mkdir(parents=True, exist_ok=True)
JOB_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "nfse-local")

JOBS: dict[str, dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()
PROCESS_LOCK = threading.Lock()


def load_ibge_municipalities() -> None:
    """
    Carrega opcionalmente um JSON no formato:

    {
        "4209102": ["Joinville", "SC"],
        "4208450": ["Itapoá", "SC"]
    }
    """
    if IBGE_MUNICIPALITIES_FILE is None:
        return

    path_value = IBGE_MUNICIPALITIES_FILE

    if not path_value.is_absolute():
        path_value = APP_DIR / path_value

    if not path_value.exists():
        print(
            f"Aviso: arquivo de municípios não encontrado: {path_value}",
            flush=True,
        )
        return

    try:
        payload = json.loads(path_value.read_text(encoding="utf-8"))
    except Exception as exc:
        print(
            f"Aviso: não foi possível ler o arquivo de municípios: {exc}",
            flush=True,
        )
        return

    for code_value, municipality_value in payload.items():
        if (
            isinstance(municipality_value, (list, tuple))
            and len(municipality_value) >= 2
        ):
            IBGE_MUNICIPALITIES[str(code_value)] = (
                str(municipality_value[0]),
                str(municipality_value[1]),
            )

        elif isinstance(municipality_value, dict):
            name = municipality_value.get("nome") or municipality_value.get("name")
            uf = municipality_value.get("uf")

            if name and uf:
                IBGE_MUNICIPALITIES[str(code_value)] = (
                    str(name),
                    str(uf),
                )


def resolve_municipality(code_value: str) -> tuple[str, str] | None:
    code = str(code_value).strip()

    if not code:
        return None

    cached = IBGE_MUNICIPALITIES.get(code)
    if cached:
        return cached

    # Consulta sob demanda à API oficial do IBGE e mantém cache local.
    # Uma falha de rede não impede a geração do documento.
    try:
        import requests

        response = requests.get(
            f"https://servicodados.ibge.gov.br/api/v1/localidades/municipios/{code}",
            timeout=8,
        )

        if response.status_code == 200:
            payload = response.json()
            name = payload.get("nome")
            uf = (
                payload.get("microrregiao", {})
                .get("mesorregiao", {})
                .get("UF", {})
                .get("sigla")
            )

            if name and uf:
                value = (str(name), str(uf))
                IBGE_MUNICIPALITIES[code] = value
                return value
    except Exception:
        pass

    return None


load_ibge_municipalities()


def update_job(job_id: str, **values: Any) -> None:
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(values)


def get_job(job_id: str) -> dict[str, Any] | None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        return dict(job) if job else None


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
        add_column_if_missing(con, "documentos", "codigo_dps", "TEXT")

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
    """
    Repete automaticamente:
    - HTTP 429;
    - HTTP 500, 502, 503 e 504;
    - falhas temporárias de conexão e validação SSL.

    A validação SSL permanece habilitada.
    """
    last_exception: Exception | None = None
    server_attempts = 0
    rate_attempts = 0
    connection_attempts = 0

    while True:
        try:
            response = certificate_get(url, params=params, accept=accept)
        except RequestException as exc:
            last_exception = exc
            connection_attempts += 1

            if connection_attempts > MAX_RETRIES_CONNECTION:
                raise

            time.sleep(RETRY_SERVER_SECONDS * connection_attempts)
            continue

        if response.status_code == 429:
            rate_attempts += 1
            if rate_attempts > MAX_RETRIES_429:
                return response

            retry_after = response.headers.get("Retry-After")
            try:
                wait_seconds = max(float(retry_after or 15), 1)
            except ValueError:
                wait_seconds = 15

            time.sleep(wait_seconds)
            continue

        if response.status_code in {500, 502, 503, 504}:
            server_attempts += 1
            if server_attempts > MAX_RETRIES_SERVER:
                return response

            time.sleep(RETRY_SERVER_SECONDS * server_attempts)
            continue

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
        "codigo_dps": None,
    }

    try:
        root = ET.fromstring(raw)

        chave = first_xml_text(
            root,
            {"chNFSe", "chaveAcesso", "ChaveAcesso"},
        )
        numero = first_xml_text(
            root,
            {"nNFSe", "numeroNFSe", "numero"},
        )
        codigo_dps = first_xml_text(
            root,
            {
                "nDPS",
                "numeroDPS",
                "codigoDPS",
                "nRPS",
                "numeroRPS",
                "codigoRPS",
            },
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
        result["codigo_dps"] = codigo_dps
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
        dps_match = re.search(
            r"<(?:\w+:)?(?:nDPS|numeroDPS|codigoDPS|nRPS|numeroRPS|codigoRPS)>([^<]+)</",
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
        result["codigo_dps"] = (
            dps_match.group(1).strip()
            if dps_match
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
                "codigo_dps": metadata["codigo_dps"],
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
    codigo_dps = document.get("codigo_dps")
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
                tipo_documento,
                codigo_dps
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                codigo_dps,
            ),
        )

    return True


def path_after_downloads(stored_value: str | Path) -> Path | None:
    stored = Path(stored_value)
    parts = list(stored.parts)
    parts_lower = [part.lower() for part in parts]

    try:
        index = parts_lower.index("downloads")
        return Path(*parts[index + 1:])
    except ValueError:
        return None


def expected_xml_path(row: sqlite3.Row) -> Path:
    """
    Sempre calcula o caminho pela instalação atual, sem depender do caminho
    absoluto que ficou gravado no computador antigo.
    """
    relative = path_after_downloads(row["caminho"])

    if relative is not None:
        return DOWNLOAD_DIR / relative

    return document_folder(row["data_emissao"]) / row["nome_arquivo"]


def current_xml_path(row: sqlite3.Row) -> Path:
    expected = expected_xml_path(row)

    if expected.exists():
        return expected

    stored = Path(row["caminho"])
    if stored.exists():
        return stored

    matches = list(DOWNLOAD_DIR.rglob(row["nome_arquivo"]))
    if matches:
        return matches[0]

    # Este é o caminho onde um arquivo novo deverá ser criado.
    return expected


def get_codigo_dps(row: sqlite3.Row) -> str | None:
    """
    Retorna o código DPS/RPS salvo no banco ou o extrai novamente do XML.
    """
    try:
        saved = row["codigo_dps"]
    except (IndexError, KeyError):
        saved = None

    if saved:
        return str(saved).strip()

    xml_path = current_xml_path(row)
    if not xml_path.exists():
        return None

    try:
        metadata = infer_xml_metadata(
            xml_path.read_bytes(),
            fallback_chave=row["chave_acesso"],
        )
        codigo = metadata.get("codigo_dps")
    except Exception:
        codigo = None

    if codigo:
        with db() as con:
            con.execute(
                "UPDATE documentos SET codigo_dps=? WHERE id=?",
                (codigo, row["id"]),
            )
        return str(codigo).strip()

    return None


def pdf_filename_for_row(row: sqlite3.Row) -> str:
    codigo_dps = get_codigo_dps(row)

    if codigo_dps:
        return safe_name(f"DPS_{codigo_dps}.pdf")

    numero_nfse = row["numero_nfse"]
    if numero_nfse:
        return safe_name(f"NFSE_{numero_nfse}.pdf")

    return safe_name(f"NFSE_NSU_{row['nsu']}.pdf")


def generated_pdf_path(row: sqlite3.Row) -> Path:
    """
    Calcula o nome definitivo do PDF lendo somente o XML selecionado.
    Não percorre nem renomeia o histórico.
    """
    xml_path = current_xml_path(row)

    if not xml_path.exists():
        raise FileNotFoundError(f"XML não encontrado: {xml_path}")

    data = parse_nfse_file(
        xml_path,
        municipality_resolver=resolve_municipality,
    )
    return xml_path.parent / data.nome_pdf()


def expected_pdf_path(row: sqlite3.Row) -> Path:
    return generated_pdf_path(row)


def current_pdf_path(row: sqlite3.Row) -> Path | None:
    """
    Reutiliza apenas o PDF local já gerado com o nome definitivo.
    """
    stored_value = row["caminho_pdf"]

    if stored_value:
        stored = Path(stored_value)

        if stored.exists():
            return stored

    try:
        expected = generated_pdf_path(row)
    except Exception:
        return None

    return expected if expected.exists() else None


def update_document_paths(
    document_id: int,
    *,
    xml_path: Path | None = None,
    pdf_path: Path | None = None,
    clear_pdf: bool = False,
) -> None:
    fields: list[str] = []
    values: list[Any] = []

    if xml_path is not None:
        fields.append("caminho=?")
        values.append(str(xml_path))

    if pdf_path is not None:
        fields.append("caminho_pdf=?")
        values.append(str(pdf_path))
    elif clear_pdf:
        fields.append("caminho_pdf=NULL")

    if not fields:
        return

    values.append(document_id)

    with db() as con:
        con.execute(
            f"UPDATE documentos SET {', '.join(fields)} WHERE id=?",
            values,
        )


def migrate_saved_paths() -> tuple[int, int]:
    """
    Regrava todos os caminhos do banco usando a pasta atual do app.

    Mesmo quando o arquivo ainda não existe, o endereço antigo é eliminado.
    """
    xml_updates = 0
    pdf_updates = 0

    with db() as con:
        rows = con.execute("SELECT * FROM documentos").fetchall()

    for row in rows:
        xml_path = expected_xml_path(row)
        pdf_path = current_pdf_path(row)

        old_xml = row["caminho"] or ""
        old_pdf = row["caminho_pdf"] or ""

        xml_changed = str(xml_path) != old_xml
        pdf_changed = False
        clear_pdf = False

        if pdf_path is not None:
            pdf_changed = str(pdf_path) != old_pdf
        elif old_pdf:
            # O banco apontava para um PDF inexistente do computador antigo.
            clear_pdf = True
            pdf_changed = True

        if xml_changed or pdf_changed:
            update_document_paths(
                row["id"],
                xml_path=xml_path if xml_changed else None,
                pdf_path=pdf_path if pdf_changed and pdf_path else None,
                clear_pdf=clear_pdf,
            )

        if xml_changed:
            xml_updates += 1
        if pdf_changed:
            pdf_updates += 1

    return xml_updates, pdf_updates



def save_pdf_for_document(
    row: sqlite3.Row,
) -> tuple[Path | None, str | None]:
    """
    Gera o DANFSe localmente a partir do XML.

    Não consulta mais o endpoint /danfse/{chave}, suspenso pelo
    Ambiente Nacional.
    """
    try:
        xml_path = current_xml_path(row)

        if not xml_path.exists():
            return None, f"XML não encontrado: {xml_path}"

        data = parse_nfse_file(
            xml_path,
            municipality_resolver=resolve_municipality,
        )

        pdf_path = xml_path.parent / data.nome_pdf()
        pdf_path.parent.mkdir(parents=True, exist_ok=True)

        if not pdf_path.exists():
            generate_danfse_pdf(
                data,
                pdf_path,
                municipality=MUNICIPALITY_INFO,
                logo_path=MUNICIPALITY_LOGO_PATH,
                options=DANFSE_OPTIONS,
            )

        update_document_paths(
            row["id"],
            xml_path=xml_path,
            pdf_path=pdf_path,
        )

        with db() as con:
            con.execute(
                """
                UPDATE documentos
                SET erro_pdf=NULL,
                    codigo_dps=?
                WHERE id=?
                """,
                (data.numero_dps, row["id"]),
            )

        return pdf_path, None

    except Exception as exc:
        error_text = str(exc)

        with db() as con:
            con.execute(
                """
                UPDATE documentos
                SET erro_pdf=?
                WHERE id=?
                """,
                (error_text, row["id"]),
            )

        return None, error_text


# ---------------------------------------------------------------------------
# Sincronização
# ---------------------------------------------------------------------------

def run_download(
    start_nsu: int,
    limit: int,
    progress_callback=None,
) -> dict[str, Any]:
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

            if progress_callback:
                progress_callback(
                    min(30, 5 + int((counters["consultados"] / max(limit, 1)) * 25)),
                    f"Consultando documentos novos (NSU {cursor_nsu})...",
                )

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
    progress_callback=None,
) -> tuple[io.BytesIO, int, list[str]]:
    rows = find_documents_by_period(data_inicial, data_final)

    if not rows:
        raise ValueError(
            "Nenhuma NFS-e foi localizada no período. "
            "Verifique se a sincronização alcançou a data selecionada."
        )

    output = io.BytesIO()
    errors: list[str] = []
    pdf_results: dict[int, Path] = {}

    if progress_callback:
        progress_callback(35, f"{len(rows)} nota(s) localizada(s). Preparando PDFs...")

    if include_pdf:
        worker_count = max(1, min(PDF_MAX_WORKERS, 12, len(rows)))

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            future_map = {
                executor.submit(save_pdf_for_document, row): row
                for row in rows
            }

            completed = 0
            for future in as_completed(future_map):
                row = future_map[future]
                completed += 1

                try:
                    pdf_path, error = future.result()
                except Exception as exc:
                    pdf_path = None
                    error = str(exc)

                if pdf_path and pdf_path.exists():
                    pdf_results[row["id"]] = pdf_path
                else:
                    identification = (
                        row["numero_nfse"]
                        or row["chave_acesso"]
                        or f"NSU {row['nsu']}"
                    )
                    error_text = error or "Erro desconhecido ao obter o PDF."
                    errors.append(f"{identification}: {error_text}")

                    with db() as con:
                        con.execute(
                            "UPDATE documentos SET erro_pdf=? WHERE id=?",
                            (error_text, row["id"]),
                        )

                if progress_callback:
                    percentage = 35 + int((completed / max(len(rows), 1)) * 55)
                    progress_callback(
                        min(90, percentage),
                        f"Baixando PDFs: {completed} de {len(rows)}...",
                    )

    if progress_callback:
        progress_callback(93, "Compactando os arquivos...")

    with zipfile.ZipFile(
        output,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=6,
    ) as archive:
        for row in rows:
            xml_path = current_xml_path(row)

            if include_xml and xml_path.exists():
                archive.write(xml_path, arcname=f"XML/{xml_path.name}")

            if include_pdf:
                pdf_path = pdf_results.get(row["id"])
                if pdf_path and pdf_path.exists():
                    archive.write(pdf_path, arcname=f"PDF/{pdf_path.name}")

        if errors:
            archive.writestr("ERROS_PDF.txt", "\n".join(errors))

    output.seek(0)
    return output, len(rows), errors


def automatic_zip_path(target_date: str) -> Path:
    year, month, _ = target_date.split("-")
    folder = AUTO_DIR / year / month
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"NFSe_{target_date}.zip"



def network_day_folder(target_date: str) -> Path | None:
    """
    Exemplo:
    NETWORK_PDF_DIR=\\\\SERVIDOR\\Financeiro\\NFS-e

    Resultado:
    \\\\SERVIDOR\\Financeiro\\NFS-e\\2026\\07\\23
    """
    if NETWORK_PDF_DIR is None:
        return None

    year, month, day = target_date.split("-")
    folder = NETWORK_PDF_DIR / year / month / day
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def copy_period_pdfs_to_network(
    data_inicial: str,
    data_final: str,
    progress_callback=None,
) -> tuple[int, list[str], str | None]:
    """
    Copia os PDFs já obtidos para a pasta de rede.
    Para execução diária, data_inicial e data_final serão o mesmo dia.
    """
    destination = network_day_folder(data_inicial)

    if destination is None:
        return 0, [], None

    if data_inicial != data_final:
        raise ValueError(
            "O salvamento direto na rede está preparado para um único dia por execução."
        )

    rows = find_documents_by_period(data_inicial, data_final)
    copied = 0
    errors: list[str] = []

    for index, row in enumerate(rows, start=1):
        pdf_path = current_pdf_path(row)
        identification = (
            row["numero_nfse"]
            or row["chave_acesso"]
            or f"NSU {row['nsu']}"
        )

        if pdf_path is None:
            errors.append(f"{identification}: PDF não disponível.")
            continue

        source = pdf_path
        if not source.exists():
            errors.append(f"{identification}: arquivo PDF não encontrado no disco.")
            continue

        try:
            target = destination / source.name
            shutil.copy2(source, target)
            copied += 1
        except Exception as exc:
            errors.append(f"{identification}: {exc}")

        if progress_callback:
            percentage = 96 + int((index / max(len(rows), 1)) * 3)
            progress_callback(
                min(99, percentage),
                f"Copiando PDFs para o servidor: {index} de {len(rows)}...",
            )

    if errors:
        try:
            (destination / "ERROS_COPIA.txt").write_text(
                "\n".join(errors),
                encoding="utf-8",
            )
        except Exception:
            pass

    return copied, errors, str(destination)


def process_period(
    data_inicial: str,
    data_final: str,
    include_xml: bool,
    include_pdf: bool,
    output_path: Path,
    progress_callback=None,
) -> dict[str, Any]:
    with PROCESS_LOCK:
        if progress_callback:
            progress_callback(2, "Iniciando sincronização...")

        sync_result = run_download(
            start_nsu=get_last_nsu() + 1,
            limit=DEFAULT_BATCH_LIMIT,
            progress_callback=progress_callback,
        )

        if progress_callback:
            progress_callback(32, "Sincronização concluída. Localizando notas do período...")

        zip_buffer, total, pdf_errors = build_period_zip(
            data_inicial,
            data_final,
            include_xml=include_xml,
            include_pdf=include_pdf,
            progress_callback=progress_callback,
        )

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(zip_buffer.getvalue())

        if progress_callback:
            progress_callback(100, "Concluído. O arquivo está pronto.")

        return {
            "arquivo": str(output_path),
            "total": total,
            "erros_pdf": len(pdf_errors),
            "sincronizacao": sync_result,
        }


def run_background_job(
    job_id: str,
    data_inicial: str,
    data_final: str,
    include_xml: bool,
    include_pdf: bool,
) -> None:
    output_path = JOB_DIR / f"{job_id}.zip"

    def progress(percent: int, message: str) -> None:
        update_job(
            job_id,
            progresso=max(0, min(100, percent)),
            mensagem=message,
        )

    try:
        result = process_period(
            data_inicial,
            data_final,
            include_xml,
            include_pdf,
            output_path,
            progress_callback=progress,
        )
        update_job(
            job_id,
            status="concluido",
            progresso=100,
            mensagem="Arquivo pronto para download.",
            arquivo=str(output_path),
            total=result["total"],
            erros_pdf=result["erros_pdf"],
        )
    except Exception as exc:
        update_job(
            job_id,
            status="erro",
            mensagem=str(exc),
        )


def run_automatic_yesterday() -> int:
    target_date = (datetime.now().date() - timedelta(days=1)).isoformat()
    output_path = automatic_zip_path(target_date)

    def console_progress(percent: int, message: str) -> None:
        print(f"[{percent:3d}%] {message}", flush=True)

    try:
        result = process_period(
            target_date,
            target_date,
            include_xml=False,
            include_pdf=True,
            output_path=output_path,
            progress_callback=console_progress,
        )

        copied, copy_errors, network_folder = copy_period_pdfs_to_network(
            target_date,
            target_date,
            progress_callback=console_progress,
        )

        network_message = (
            f" | Rede: {network_folder} | {copied} PDF(s) copiado(s) "
            f"| {len(copy_errors)} erro(s) de cópia"
            if network_folder
            else " | Pasta de rede não configurada"
        )

        print(
            f"Concluído: {output_path} | "
            f"{result['total']} nota(s) | "
            f"{result['erros_pdf']} erro(s) de PDF"
            f"{network_message}.",
            flush=True,
        )
        return 0
    except Exception as exc:
        print(f"ERRO: {exc}", flush=True)
        return 1


# ---------------------------------------------------------------------------
# Rotas
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    ontem = (datetime.now().date()).isoformat()
    try:
        from datetime import timedelta
        ontem = (datetime.now().date() - timedelta(days=1)).isoformat()
    except Exception:
        pass

    data_inicial = request.args.get("data_inicial", ontem)
    data_final = request.args.get("data_final", ontem)

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
        automatico_ontem=automatic_zip_path(ontem).exists(),
        ontem=ontem,
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


@app.post("/api/iniciar-download")
def iniciar_download_api():
    try:
        payload = request.get_json(silent=True) or request.form
        data_inicial, data_final = validate_period(
            payload.get("data_inicial"),
            payload.get("data_final"),
        )
        include_xml = str(payload.get("xml", "0")) == "1"
        include_pdf = str(payload.get("pdf", "1")) == "1"

        if not include_xml and not include_pdf:
            raise ValueError("Escolha pelo menos XML ou PDF.")

        with JOBS_LOCK:
            running = any(
                job.get("status") in {"aguardando", "processando"}
                for job in JOBS.values()
            )
            if running:
                return {
                    "erro": "Já existe um download em processamento. Aguarde a conclusão."
                }, 409

            job_id = uuid.uuid4().hex
            JOBS[job_id] = {
                "id": job_id,
                "status": "processando",
                "progresso": 0,
                "mensagem": "Aguardando início...",
                "data_inicial": data_inicial,
                "data_final": data_final,
                "arquivo": None,
            }

        thread = threading.Thread(
            target=run_background_job,
            args=(job_id, data_inicial, data_final, include_xml, include_pdf),
            daemon=True,
        )
        thread.start()

        return {"job_id": job_id}

    except Exception as exc:
        return {"erro": str(exc)}, 400


@app.get("/api/progresso/<job_id>")
def progresso_api(job_id: str):
    job = get_job(job_id)
    if not job:
        return {"erro": "Processamento não encontrado."}, 404
    return job


@app.get("/api/download/<job_id>")
def download_job_api(job_id: str):
    job = get_job(job_id)

    if not job:
        return "Processamento não encontrado.", 404

    if job.get("status") != "concluido":
        return "O arquivo ainda não está pronto.", 409

    path = Path(job["arquivo"])
    if not path.exists():
        return "Arquivo não encontrado.", 404

    filename = (
        f"NFSe_{job['data_inicial']}_a_{job['data_final']}.zip"
    )
    return send_file(
        path,
        mimetype="application/zip",
        as_attachment=True,
        download_name=filename,
    )


@app.get("/automatico/<data>")
def download_automatico(data: str):
    try:
        validate_period(data, data)
        path = automatic_zip_path(data)
        if not path.exists():
            return "Arquivo automático não encontrado para essa data.", 404
        return send_file(
            path,
            mimetype="application/zip",
            as_attachment=True,
            download_name=path.name,
        )
    except ValueError as exc:
        return str(exc), 400


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

    path = current_xml_path(row)

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


@app.route("/teste-pdf-local/<int:doc_id>")
def teste_pdf_local(doc_id: int):
    """
    Gera e baixa localmente o DANFSe de um documento do banco.
    """
    with db() as con:
        row = con.execute(
            "SELECT * FROM documentos WHERE id=?",
            (doc_id,),
        ).fetchone()

    if not row:
        return "Documento não encontrado.", 404

    pdf_path, error = save_pdf_for_document(row)

    if not pdf_path:
        return f"Não foi possível gerar o PDF: {error}", 500

    return send_from_directory(
        pdf_path.parent,
        pdf_path.name,
        as_attachment=True,
        download_name=pdf_path.name,
    )


if __name__ == "__main__":
    init_db()

    # Não percorre nem renomeia o histórico na inicialização.
    # PDFs antigos são ignorados; quando necessário, o sistema baixa
    # novamente o DANFSe e salva diretamente com o nome DPS/RPS.
    print("NFSe Downloader Final - DANFSe local NT 008 v1.02.", flush=True)

    if "--automatico" in sys.argv:
        raise SystemExit(run_automatic_yesterday())

    app.run(
        host="127.0.0.1",
        port=5000,
        debug=False,
        threaded=True,
    )
