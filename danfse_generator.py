from __future__ import annotations

import io
import os
import textwrap
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

import qrcode
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas
from reportlab.lib.utils import ImageReader

from nfse_xml import (
    NFSeData,
    Pessoa,
    decimal_to_br,
    money_br,
    parse_nfse_file,
    percent_br,
)


# =============================================================================
# Configurações da NT 008 v1.02
# =============================================================================

PAGE_WIDTH, PAGE_HEIGHT = A4

# A NT permite margens entre 0,15 cm e 0,20 cm.
PAGE_MARGIN = 0.18 * cm

# Corpo principal sugerido pelo manual.
BODY_LEFT = 0.30 * cm
BODY_TOP = 0.30 * cm
BODY_WIDTH = 20.40 * cm

# Linhas e sombreamento.
OUTER_LINE_WIDTH = 1.0
INNER_LINE_WIDTH = 0.5
LIGHT_GRAY = colors.Color(0.95, 0.95, 0.95)  # cinza 5%
WATERMARK_GRAY = colors.Color(0.65, 0.65, 0.65)  # aproximação K35
BLACK = colors.black
RED = colors.Color(1, 0, 0)

# Fontes e tamanhos mínimos obrigatórios.
BLOCK_LABEL_SIZE = 7
FIELD_LABEL_SIZE = 6
IDENT_FIELD_LABEL_SIZE = 7
CONTENT_SIZE = 7
HEADER_SIZE = 9
MUNICIPALITY_SIZE = 8
HEADER_META_SIZE = 6
QR_TEXT_SIZE = 6
WATERMARK_SIZE = 50

# Dimensões obrigatórias do QR Code.
QR_SIZE = 1.52 * cm
QR_X = 17.48 * cm
QR_TOP = 1.67 * cm

# Alturas sugeridas na NT.
H_032 = 0.32 * cm
H_038 = 0.38 * cm
H_039 = 0.39 * cm
H_063 = 0.63 * cm
H_067 = 0.67 * cm

# Colunas de 5,09 / 10,19 / 15,30 / 20,40 cm.
COL = 5.09 * cm
COL2 = 10.19 * cm
COL3 = 15.30 * cm
FULL = 20.40 * cm

QR_URL = "https://www.nfse.gov.br/ConsultaPublica/?tpc=1&chave="

APP_DIR = Path(__file__).resolve().parent
GENERATOR_BUILD = "NT008-1.02-LAYOUT-VALIDADO-2026-08-04"
DEFAULT_LOGO_PATH = APP_DIR / "logo-nfs-e-horizontal.png"


# =============================================================================
# Fontes
# =============================================================================

FONT_ARIAL = "Arial"
FONT_ARIAL_BOLD = "Arial-Bold"
FONT_MS_SANS = "MicrosoftSansSerif"
FONT_MS_SANS_BOLD = "MicrosoftSansSerif-Bold"

_FONT_REGISTERED = False


def _register_font_if_found(name: str, candidates: list[Path]) -> bool:
    for candidate in candidates:
        # Path("") resulta em ".", portanto ignoramos diretórios e valores vazios.
        if not candidate or not candidate.is_file():
            continue

        pdfmetrics.registerFont(TTFont(name, str(candidate)))
        return True

    return False


def register_required_fonts() -> None:
    """
    Registra Arial e Microsoft Sans Serif a partir das fontes instaladas
    no Windows. Não distribui arquivos de fonte.

    Em outro sistema operacional, podem ser informados caminhos por:
      DANFSE_ARIAL_PATH
      DANFSE_ARIAL_BOLD_PATH
      DANFSE_MS_SANS_PATH
      DANFSE_MS_SANS_BOLD_PATH
    """
    global _FONT_REGISTERED

    if _FONT_REGISTERED:
        return

    windows_fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"

    arial_ok = _register_font_if_found(
        FONT_ARIAL,
        [
            Path(os.getenv("DANFSE_ARIAL_PATH", "")),
            windows_fonts / "arial.ttf",
        ],
    )

    arial_bold_ok = _register_font_if_found(
        FONT_ARIAL_BOLD,
        [
            Path(os.getenv("DANFSE_ARIAL_BOLD_PATH", "")),
            windows_fonts / "arialbd.ttf",
        ],
    )

    ms_sans_ok = _register_font_if_found(
        FONT_MS_SANS,
        [
            Path(os.getenv("DANFSE_MS_SANS_PATH", "")),
            windows_fonts / "micross.ttf",
            windows_fonts / "micross.TTF",
        ],
    )

    ms_sans_bold_ok = _register_font_if_found(
        FONT_MS_SANS_BOLD,
        [
            Path(os.getenv("DANFSE_MS_SANS_BOLD_PATH", "")),
            windows_fonts / "microssb.ttf",
            windows_fonts / "microssbd.ttf",
        ],
    )

    # Fallbacks somente para evitar interrupção. Para conformidade formal,
    # as fontes exigidas devem estar disponíveis.
    if not arial_ok:
        globals()["FONT_ARIAL"] = "Helvetica"
    if not arial_bold_ok:
        globals()["FONT_ARIAL_BOLD"] = "Helvetica-Bold"
    if not ms_sans_ok:
        globals()["FONT_MS_SANS"] = "Helvetica"
    if not ms_sans_bold_ok:
        globals()["FONT_MS_SANS_BOLD"] = "Helvetica-Bold"

    _FONT_REGISTERED = True


# =============================================================================
# Dados adicionais do município e opções
# =============================================================================

@dataclass(slots=True)
class MunicipalityInfo:
    name: str = "Prefeitura de Joinville"
    phone: str = "(47) 3431-3233"
    email: str = "fiscalville@joinville.sc.gov.br"
    logo_path: Path | None = DEFAULT_LOGO_PATH


@dataclass(slots=True)
class GeneratorOptions:
    include_stub: bool = False
    suppress_optional_address_lines: bool = False
    strict_fonts: bool = False
    strict_logo: bool = True
    strict_one_page: bool = True


# =============================================================================
# Utilidades
# =============================================================================

def _value(obj: Any, name: str, default: Any = "") -> Any:
    if obj is None:
        return default
    return getattr(obj, name, default)


def _dash(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, Decimal):
        return "-" if value == Decimal("0") else money_br(value)
    text = str(value).strip()
    return text if text else "-"


def _money(value: Any) -> str:
    try:
        decimal = value if isinstance(value, Decimal) else Decimal(str(value or "0"))
    except Exception:
        decimal = Decimal("0")
    return "-" if decimal == Decimal("0") else money_br(decimal)


def _percent(value: Any) -> str:
    try:
        decimal = value if isinstance(value, Decimal) else Decimal(str(value or "0"))
    except Exception:
        decimal = Decimal("0")
    return "-" if decimal == Decimal("0") else percent_br(decimal)


def _truncate(text: str, maximum: int) -> str:
    normalized = " ".join((text or "").split())
    if len(normalized) <= maximum:
        return normalized
    return normalized[: max(0, maximum - 3)].rstrip() + "..."


def _top_to_y(top_cm: float, height_cm: float = 0) -> float:
    return PAGE_HEIGHT - (top_cm + height_cm) * cm


def _string_width(text: str, font: str, size: float) -> float:
    return pdfmetrics.stringWidth(text, font, size)


def _wrap_lines(
    text: str,
    font: str,
    size: float,
    max_width: float,
    max_lines: int,
) -> list[str]:
    normalized = " ".join((text or "").split())

    if not normalized:
        return ["-"]

    words = normalized.split()
    lines: list[str] = []
    current = ""

    for word in words:
        candidate = word if not current else f"{current} {word}"

        if _string_width(candidate, font, size) <= max_width:
            current = candidate
            continue

        if current:
            lines.append(current)

        current = word

        if len(lines) >= max_lines:
            break

    if current and len(lines) < max_lines:
        lines.append(current)

    if not lines:
        lines = ["-"]

    joined_original = " ".join(words)
    joined_lines = " ".join(lines)

    if len(joined_lines) < len(joined_original):
        last = lines[-1]
        while last and _string_width(last + "...", font, size) > max_width:
            last = last[:-1]
        lines[-1] = last.rstrip() + "..."

    return lines[:max_lines]


def _fit_font_size(
    text: str,
    font: str,
    desired_size: float,
    max_width: float,
    *,
    minimum_size: float = 4.5,
) -> float:
    """Reduz a fonte apenas quando necessário para caber em uma linha."""
    size = desired_size
    while size > minimum_size and _string_width(text, font, size) > max_width:
        size -= 0.25
    return max(size, minimum_size)


def _ellipsize_to_width(
    text: str,
    font: str,
    size: float,
    max_width: float,
) -> str:
    normalized = " ".join((text or "").split())
    if not normalized:
        return "-"
    if _string_width(normalized, font, size) <= max_width:
        return normalized
    suffix = "..."
    candidate = normalized
    while candidate and _string_width(candidate + suffix, font, size) > max_width:
        candidate = candidate[:-1]
    return candidate.rstrip() + suffix


def _clip_to_box(canvas: Canvas, x: float, y: float, width: float, height: float) -> None:
    path_obj = canvas.beginPath()
    path_obj.rect(x, y, width, height)
    canvas.clipPath(path_obj, stroke=0, fill=0)


def _draw_multiline(
    canvas: Canvas,
    text: str,
    x: float,
    top_y: float,
    width: float,
    height: float,
    *,
    font: str = FONT_MS_SANS,
    size: float = CONTENT_SIZE,
    max_lines: int = 2,
    leading: float | None = None,
    align: str = "left",
    color: colors.Color = BLACK,
) -> None:
    """Desenha texto dentro de uma caixa e impede qualquer vazamento."""
    if width <= 0 or height <= 0:
        return

    leading = leading or (size + 0.8)
    physical_lines = max(1, int((height + 0.2) // leading))
    effective_lines = max(1, min(max_lines, physical_lines))
    lines = _wrap_lines(text, font, size, width, effective_lines)

    bottom_y = top_y - height
    canvas.saveState()
    _clip_to_box(canvas, x, bottom_y, width, height)
    canvas.setFillColor(color)
    canvas.setFont(font, size)

    # Baseline superior com folga; não toca a borda.
    y = top_y - size - 0.15

    for line in lines:
        if y < bottom_y + 0.05:
            break
        if align == "center":
            canvas.drawCentredString(x + width / 2, y, line)
        elif align == "right":
            canvas.drawRightString(x + width, y, line)
        else:
            canvas.drawString(x, y, line)
        y -= leading

    canvas.restoreState()


def _draw_rect(
    canvas: Canvas,
    x: float,
    top: float,
    width: float,
    height: float,
    *,
    fill: colors.Color | None = None,
    line_width: float = INNER_LINE_WIDTH,
) -> None:
    y = PAGE_HEIGHT - top - height

    canvas.saveState()
    canvas.setLineWidth(line_width)
    canvas.setStrokeColor(BLACK)

    if fill is not None:
        canvas.setFillColor(fill)
        canvas.rect(x, y, width, height, stroke=1, fill=1)
    else:
        canvas.rect(x, y, width, height, stroke=1, fill=0)

    canvas.restoreState()


def _draw_block_title(
    canvas: Canvas,
    title: str,
    top: float,
    *,
    height: float = H_032,
) -> float:
    _draw_rect(canvas, BODY_LEFT, top, BODY_WIDTH, height, fill=LIGHT_GRAY)

    y = PAGE_HEIGHT - top - height
    text = title.upper()
    available_width = BODY_WIDTH - 0.16 * cm
    size = _fit_font_size(text, FONT_ARIAL_BOLD, BLOCK_LABEL_SIZE, available_width)
    text = _ellipsize_to_width(text, FONT_ARIAL_BOLD, size, available_width)

    canvas.saveState()
    _clip_to_box(canvas, BODY_LEFT, y, BODY_WIDTH, height)
    canvas.setFillColor(BLACK)
    canvas.setFont(FONT_ARIAL_BOLD, size)
    baseline = y + max(1.2, (height - size) / 2 + 0.8)
    canvas.drawString(BODY_LEFT + 0.08 * cm, baseline, text)
    canvas.restoreState()

    return top + height


def _draw_field(
    canvas: Canvas,
    label: str,
    value: str,
    x: float,
    top: float,
    width: float,
    height: float,
    *,
    shaded: bool = False,
    label_size: float = FIELD_LABEL_SIZE,
    content_size: float = CONTENT_SIZE,
    label_upper: bool = False,
    max_chars: int | None = None,
    max_lines: int = 2,
    centered: bool = False,
) -> None:
    """Célula do DANFSe com label e conteúdo em áreas separadas e recortadas."""
    _draw_rect(
        canvas,
        x,
        top,
        width,
        height,
        fill=LIGHT_GRAY if shaded else None,
    )

    bottom = PAGE_HEIGHT - top - height
    inner_x = x + 0.07 * cm
    inner_w = max(1, width - 0.14 * cm)
    inner_bottom = bottom + 0.04 * cm
    inner_h = max(1, height - 0.08 * cm)

    label_text = (label.upper() if label_upper else label).strip()
    value_text = _truncate(str(value or ""), max_chars) if max_chars else str(value or "")
    value_text = _dash(value_text)

    canvas.saveState()
    _clip_to_box(canvas, x + 0.01 * cm, bottom + 0.01 * cm, width - 0.02 * cm, height - 0.02 * cm)

    if not label_text:
        # Campos sem label (ex.: descrição tributária) usam toda a altura.
        effective_size = min(content_size, max(5.5, height / 2.8))
        _draw_multiline(
            canvas,
            value_text,
            inner_x,
            bottom + height - 0.05 * cm,
            inner_w,
            inner_h,
            font=FONT_MS_SANS,
            size=effective_size,
            max_lines=max_lines,
            align="center" if centered else "left",
        )
        canvas.restoreState()
        return

    # A faixa do label é independente da faixa do conteúdo.
    label_band = min(7.4, max(5.4, height * 0.38))
    value_band = max(2.0, inner_h - label_band - 0.5)

    fitted_label_size = _fit_font_size(
        label_text,
        FONT_ARIAL_BOLD,
        label_size,
        inner_w,
        minimum_size=4.25,
    )
    label_text = _ellipsize_to_width(
        label_text,
        FONT_ARIAL_BOLD,
        fitted_label_size,
        inner_w,
    )

    canvas.setFillColor(BLACK)
    canvas.setFont(FONT_ARIAL_BOLD, fitted_label_size)
    label_baseline = bottom + height - fitted_label_size - 1.6
    canvas.drawString(inner_x, label_baseline, label_text)

    # Conteúdo nunca invade a faixa do label e nunca cruza a borda inferior.
    value_top = bottom + value_band + 1.0
    physical_max_lines = max(1, int(value_band // (content_size + 0.8)))
    requested_lines = min(max_lines, physical_max_lines)

    _draw_multiline(
        canvas,
        value_text,
        inner_x,
        value_top,
        inner_w,
        value_band,
        font=FONT_MS_SANS,
        size=content_size,
        max_lines=max(1, requested_lines),
        align="center" if centered else "left",
    )

    canvas.restoreState()

def _draw_message_block(
    canvas: Canvas,
    message: str,
    top: float,
    *,
    height: float = H_032,
) -> float:
    _draw_rect(canvas, BODY_LEFT, top, BODY_WIDTH, height, fill=LIGHT_GRAY)
    bottom = PAGE_HEIGHT - top - height
    text = message.upper()
    available = BODY_WIDTH - 0.16 * cm
    size = _fit_font_size(text, FONT_ARIAL_BOLD, BLOCK_LABEL_SIZE, available)
    text = _ellipsize_to_width(text, FONT_ARIAL_BOLD, size, available)

    canvas.saveState()
    _clip_to_box(canvas, BODY_LEFT, bottom, BODY_WIDTH, height)
    canvas.setFont(FONT_ARIAL_BOLD, size)
    canvas.drawString(
        BODY_LEFT + 0.08 * cm,
        bottom + max(1.2, (height - size) / 2 + 0.8),
        text,
    )
    canvas.restoreState()

    return top + height


def _person_is_present(person: Pessoa | None) -> bool:
    if person is None:
        return False

    return any(
        (
            _value(person, "documento"),
            _value(person, "nome"),
            _value(person, "inscricao_municipal"),
            _value(person, "telefone"),
            _value(person, "email"),
        )
    )


def _person_same(first: Pessoa | None, second: Pessoa | None) -> bool:
    if not _person_is_present(first) or not _person_is_present(second):
        return False

    first_doc = "".join(filter(str.isdigit, _value(first, "documento")))
    second_doc = "".join(filter(str.isdigit, _value(second, "documento")))

    return bool(first_doc and first_doc == second_doc)


def _draw_person_block(
    canvas: Canvas,
    title: str,
    person: Pessoa,
    top: float,
    *,
    include_sn: bool = False,
    simples_nacional: str = "",
    regime_apuracao: str = "",
    suppress_optional_lines: bool = False,
) -> float:
    top = _draw_block_title(canvas, title, top, height=H_032)

    x0 = BODY_LEFT

    _draw_field(
        canvas,
        "CNPJ / CPF / NIF",
        _value(person, "documento"),
        x0,
        top,
        COL,
        H_063,
        max_chars=40,
    )
    _draw_field(
        canvas,
        "Indicador Municipal (Inscrição)",
        _value(person, "inscricao_municipal"),
        x0 + COL,
        top,
        COL,
        H_063,
        max_chars=15,
    )
    _draw_field(
        canvas,
        "Telefone",
        _value(person, "telefone"),
        x0 + COL * 2,
        top,
        COL,
        H_063,
        max_chars=20,
    )
    _draw_field(
        canvas,
        "Nome / Nome Empresarial",
        _value(person, "nome"),
        x0 + COL * 3,
        top,
        BODY_WIDTH - COL * 3,
        H_063,
        max_chars=80,
    )
    top += H_063

    address = _value(person, "endereco", None)

    _draw_field(
        canvas,
        "Município / Sigla UF",
        _value(address, "municipio_uf"),
        x0,
        top,
        COL,
        H_063,
        max_chars=40,
    )
    _draw_field(
        canvas,
        "Código IBGE / CEP",
        " / ".join(
            item
            for item in (
                _value(address, "codigo_municipio"),
                _value(address, "cep"),
            )
            if item
        ),
        x0 + COL,
        top,
        COL,
        H_063,
        max_chars=21,
    )
    _draw_field(
        canvas,
        "Endereço",
        _value(address, "linha"),
        x0 + COL * 2,
        top,
        BODY_WIDTH - COL * 2,
        H_063,
        max_chars=80,
    )
    top += H_063

    if not suppress_optional_lines:
        _draw_field(
            canvas,
            "E-mail",
            _value(person, "email"),
            x0,
            top,
            COL2,
            H_063,
            max_chars=80,
        )

        if include_sn:
            _draw_field(
                canvas,
                "Simples Nacional na Data de Competência",
                simples_nacional,
                x0 + COL2,
                top,
                COL,
                H_063,
                max_chars=40,
            )
            _draw_field(
                canvas,
                "Regime de Apuração Tributária pelo SN",
                regime_apuracao,
                x0 + COL2 + COL,
                top,
                BODY_WIDTH - COL2 - COL,
                H_063,
                max_chars=80,
            )
        else:
            _draw_field(
                canvas,
                "E-mail",
                _value(person, "email"),
                x0,
                top,
                BODY_WIDTH,
                H_063,
                max_chars=80,
            )

        top += H_063

    elif include_sn:
        _draw_field(
            canvas,
            "Simples Nacional na Data de Competência",
            simples_nacional,
            x0,
            top,
            COL2,
            H_063,
            max_chars=40,
        )
        _draw_field(
            canvas,
            "Regime de Apuração Tributária pelo SN",
            regime_apuracao,
            x0 + COL2,
            top,
            BODY_WIDTH - COL2,
            H_063,
            max_chars=80,
        )
        top += H_063

    return top


def _create_qr(chave: str) -> io.BytesIO:
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=1,
    )
    qr.add_data(QR_URL + chave)
    qr.make(fit=True)

    image = qr.make_image(fill_color="black", back_color="white")

    output = io.BytesIO()
    image.save(output, format="PNG")
    output.seek(0)
    return output


# =============================================================================
# Cabeçalho e identificação
# =============================================================================

def _resolve_logo_path(
    municipality: MunicipalityInfo,
    explicit_logo_path: Path | None,
) -> Path | None:
    """
    Localiza a logo oficial logo-nfs-e-horizontal.png.

    A busca considera:
    1. caminho explícito;
    2. MunicipalityInfo.logo_path;
    3. pasta do danfse_generator.py;
    4. diretório atual de execução;
    5. pasta pai do programa;
    6. busca sem diferenciar maiúsculas/minúsculas.
    """
    candidates: list[Path] = []

    if explicit_logo_path:
        candidates.append(Path(explicit_logo_path))

    if municipality.logo_path:
        candidates.append(Path(municipality.logo_path))

    candidates.extend(
        [
            APP_DIR / "logo-nfs-e-horizontal.png",
            Path.cwd() / "logo-nfs-e-horizontal.png",
            APP_DIR.parent / "logo-nfs-e-horizontal.png",
        ]
    )

    checked_directories: set[Path] = set()

    for candidate in candidates:
        resolved = candidate.expanduser()

        if not resolved.is_absolute():
            resolved = APP_DIR / resolved

        try:
            resolved = resolved.resolve()
        except OSError:
            pass

        if resolved.is_file():
            return resolved

        parent = resolved.parent

        if parent in checked_directories or not parent.is_dir():
            continue

        checked_directories.add(parent)

        expected_name = resolved.name.casefold()

        for item in parent.iterdir():
            if item.is_file() and item.name.casefold() == expected_name:
                return item

    return None



def _draw_header(
    canvas: Canvas,
    data: NFSeData,
    municipality: MunicipalityInfo,
    logo_path: Path | None,
    *,
    strict_logo: bool,
) -> None:
    top = 0.30 * cm
    height = 1.16 * cm

    _draw_rect(canvas, BODY_LEFT, top, BODY_WIDTH, height, fill=LIGHT_GRAY)

    # Logomarca horizontal oficial da NFS-e.
    # Dimensões e posição previstas na NT 008 v1.02.
    resolved_logo = _resolve_logo_path(
        municipality,
        logo_path,
    )

    logo_box_x = 0.49 * cm
    logo_box_top = 0.44 * cm
    logo_box_w = 4.00 * cm
    logo_box_h = 0.85 * cm
    logo_box_y = PAGE_HEIGHT - logo_box_top - logo_box_h

    if resolved_logo is not None:
        canvas.drawImage(
            str(resolved_logo),
            logo_box_x,
            logo_box_y,
            width=logo_box_w,
            height=logo_box_h,
            preserveAspectRatio=True,
            anchor="c",
            mask="auto",
        )
    else:
        if strict_logo:
            raise FileNotFoundError(
                "A logomarca obrigatória não foi encontrada. "
                "Salve o arquivo 'logo-nfs-e-horizontal.png' na mesma "
                "pasta de app.py e danfse_generator.py."
            )

        canvas.saveState()
        canvas.setFont(FONT_ARIAL_BOLD, 16)
        canvas.drawCentredString(
            logo_box_x + logo_box_w / 2,
            logo_box_y + 0.28 * cm,
            "NFS-e",
        )
        canvas.restoreState()

    # Centro.
    center_x = 5.41 * cm
    center_w = 10.19 * cm

    canvas.saveState()
    canvas.setFont(FONT_ARIAL_BOLD, HEADER_SIZE)
    canvas.drawCentredString(
        center_x + center_w / 2,
        PAGE_HEIGHT - top - 0.38 * cm,
        "DANFSe v2.0",
    )
    canvas.drawCentredString(
        center_x + center_w / 2,
        PAGE_HEIGHT - top - 0.66 * cm,
        "Documento Auxiliar da NFS-e",
    )

    if _value(data, "homologacao", False):
        canvas.setFillColor(RED)
        canvas.drawCentredString(
            center_x + center_w / 2,
            PAGE_HEIGHT - top - 1.02 * cm,
            "NFS-e SEM VALIDADE JURÍDICA",
        )

    canvas.restoreState()

    # Município / ambiente.
    right_x = 15.62 * cm
    right_w = 5.09 * cm

    municipality_name = municipality.name
    if _value(data, "municipio_emitente"):
        municipality_name = (
            f"Município: {_value(data, 'municipio_emitente')}"
            f" / {_value(data, 'uf_emitente', '-') or '-'}"
        )

    _draw_multiline(
        canvas,
        municipality_name,
        right_x + 0.08 * cm,
        PAGE_HEIGHT - top - 0.10 * cm,
        right_w - 0.16 * cm,
        0.54 * cm,
        font=FONT_MS_SANS,
        size=MUNICIPALITY_SIZE,
        max_lines=2,
        align="center",
    )

    _draw_multiline(
        canvas,
        f"Ambiente gerador: {_dash(_value(data, 'ambiente_gerador'))}",
        right_x + 0.08 * cm,
        PAGE_HEIGHT - top - 0.72 * cm,
        right_w - 0.16 * cm,
        0.22 * cm,
        font=FONT_MS_SANS,
        size=HEADER_META_SIZE,
        max_lines=1,
        align="center",
    )

    _draw_multiline(
        canvas,
        f"Ambiente: {_dash(_value(data, 'ambiente'))}",
        right_x + 0.08 * cm,
        PAGE_HEIGHT - top - 0.90 * cm,
        right_w - 0.16 * cm,
        0.22 * cm,
        font=FONT_MS_SANS,
        size=HEADER_META_SIZE,
        max_lines=1,
        align="center",
    )


def _draw_identification(canvas: Canvas, data: NFSeData) -> float:
    top = 1.48 * cm
    section_height = 2.84 * cm

    _draw_rect(canvas, BODY_LEFT, top, BODY_WIDTH, section_height)

    # Chave.
    _draw_field(
        canvas,
        "CHAVE DE ACESSO DA NFS-E",
        _value(data, "chave"),
        BODY_LEFT,
        top,
        15.30 * cm,
        0.77 * cm,
        label_size=IDENT_FIELD_LABEL_SIZE,
        label_upper=True,
        max_chars=50,
    )

    # QR Code no posicionamento indicado.
    qr = _create_qr(_value(data, "chave"))
    canvas.drawImage(
        ImageReader(qr),
        QR_X,
        PAGE_HEIGHT - QR_TOP - QR_SIZE,
        width=QR_SIZE,
        height=QR_SIZE,
        preserveAspectRatio=True,
        mask="auto",
    )

    qr_text_top = 3.36 * cm
    qr_text_x = 15.80 * cm
    qr_text_w = 4.72 * cm
    qr_text_h = 0.68 * cm
    qr_text_bottom = PAGE_HEIGHT - qr_text_top - qr_text_h

    canvas.saveState()
    _clip_to_box(canvas, qr_text_x, qr_text_bottom, qr_text_w, qr_text_h)
    canvas.setFont(FONT_MS_SANS, QR_TEXT_SIZE)
    qr_lines = (
        "A autenticidade desta NFS-e pode ser verificada",
        "pela leitura deste código QR ou pela consulta da",
        "chave de acesso no portal nacional da NFS-e",
    )
    qr_y = PAGE_HEIGHT - qr_text_top - QR_TEXT_SIZE
    for qr_line in qr_lines:
        canvas.drawCentredString(qr_text_x + qr_text_w / 2, qr_y, qr_line)
        qr_y -= 6.8
    canvas.restoreState()

    row_top = 2.27 * cm

    fields = [
        ("NÚMERO DA NFS-E", _value(data, "numero_nfse")),
        ("COMPETÊNCIA DA NFS-E", _value(data, "competencia")),
        ("DATA E HORA DA EMISSÃO DA NFS-E", _value(data, "emissao_nfse")),
        ("NÚMERO DA DPS", _value(data, "numero_dps")),
        ("SÉRIE DA DPS", _value(data, "serie_dps")),
        ("DATA E HORA DA EMISSÃO DA DPS", _value(data, "emissao_dps")),
        ("EMITENTE DA NFS-E", _value(data, "emitente_nfse")),
        ("SITUAÇÃO DA NFS-E", _value(data, "situacao")),
        ("FINALIDADE", _value(data, "finalidade")),
    ]

    for row in range(3):
        for column in range(3):
            label, value = fields[row * 3 + column]
            _draw_field(
                canvas,
                label,
                value,
                BODY_LEFT + COL * column,
                row_top + H_067 * row,
                COL,
                H_067,
                shaded=(label == "EMITENTE DA NFS-E"),
                label_size=IDENT_FIELD_LABEL_SIZE,
                label_upper=True,
                max_chars=40,
            )

    return 4.34 * cm


# =============================================================================
# Serviço, tributos e valores
# =============================================================================

def _draw_service(canvas: Canvas, data: NFSeData, top: float) -> float:
    service = _value(data, "servico", None)

    top = _draw_block_title(canvas, "SERVIÇO PRESTADO", top)

    code = " / ".join(
        item
        for item in (
            _value(service, "codigo_tributacao_nacional"),
            _value(service, "codigo_tributacao_municipal"),
        )
        if item
    )

    location = " / ".join(
        item
        for item in (
            _value(service, "local_prestacao"),
            _value(service, "uf_prestacao"),
            _value(service, "pais_prestacao"),
        )
        if item
    )

    _draw_field(
        canvas,
        "Código de Tributação Nacional / Municipal",
        code,
        BODY_LEFT,
        top,
        COL,
        H_063,
        max_chars=14,
    )
    _draw_field(
        canvas,
        "Código da NBS",
        _value(service, "codigo_nbs"),
        BODY_LEFT + COL,
        top,
        COL,
        H_063,
        max_chars=12,
    )
    _draw_field(
        canvas,
        "Local da Prestação / Sigla UF / País",
        location,
        BODY_LEFT + COL * 2,
        top,
        BODY_WIDTH - COL * 2,
        H_063,
        max_chars=42,
    )
    top += H_063

    description_code = (
        _value(service, "descricao_tributacao_municipal")
        or _value(service, "descricao_tributacao_nacional")
    )

    _draw_field(
        canvas,
        "",
        description_code,
        BODY_LEFT,
        top,
        BODY_WIDTH,
        H_038,
        max_chars=170,
        max_lines=1,
    )
    top += H_038

    # A altura mínima é 0,63cm, mas ampliamos moderadamente para legibilidade.
    service_height = 1.05 * cm

    _draw_field(
        canvas,
        "Descrição do Serviço",
        _value(service, "descricao"),
        BODY_LEFT,
        top,
        BODY_WIDTH,
        service_height,
        max_chars=1300,
        max_lines=5,
    )
    top += service_height

    return top


def _draw_municipal_tax(canvas: Canvas, data: NFSeData, top: float) -> float:
    tax = _value(data, "tributacao_municipal", None)

    subject = _value(tax, "tributacao_issqn")

    if not subject or str(subject).strip() in {"3", "4"}:
        return _draw_message_block(
            canvas,
            "TRIBUTAÇÃO MUNICIPAL (ISSQN) - OPERAÇÃO NÃO SUJEITA AO ISSQN",
            top,
        )

    top = _draw_block_title(canvas, "TRIBUTAÇÃO MUNICIPAL (ISSQN)", top)

    incidence = " / ".join(
        item
        for item in (
            _value(tax, "municipio_incidencia"),
            _value(tax, "uf_incidencia"),
            _value(tax, "pais_resultado"),
        )
        if item
    )

    # Linha 1.
    _draw_field(
        canvas,
        "Tipo de Tributação do ISSQN",
        subject,
        BODY_LEFT,
        top,
        COL,
        H_063,
        max_chars=21,
    )
    _draw_field(
        canvas,
        "Município / Sigla UF / País da Incidência do ISSQN",
        incidence,
        BODY_LEFT + COL,
        top,
        BODY_WIDTH - COL,
        H_063,
        max_chars=42,
    )
    top += H_063

    # Linha opcional, suprimível se todos vazios.
    optional_values = [
        _value(tax, "regime_especial"),
        _value(tax, "imunidade"),
        _value(tax, "suspensao"),
        _value(tax, "numero_processo_suspensao"),
    ]

    if any(str(item or "").strip() for item in optional_values):
        labels = [
            ("Regime Especial de Tributação do ISSQN", optional_values[0]),
            ("Tipo de Imunidade do ISSQN", optional_values[1]),
            ("Suspensão da Exigibilidade do ISSQN", optional_values[2]),
            ("Número Processo Suspensão", optional_values[3]),
        ]
        for index, (label, value) in enumerate(labels):
            _draw_field(
                canvas,
                label,
                value,
                BODY_LEFT + COL * index,
                top,
                COL,
                H_063,
                max_chars=40,
            )
        top += H_063

    # Linha opcional BM/deduções.
    optional_amounts = [
        _value(tax, "beneficio_municipal"),
        _value(tax, "calculo_beneficio_municipal", Decimal("0")),
        _value(tax, "total_deducoes_reducoes", Decimal("0")),
        _value(tax, "desconto_incondicionado", Decimal("0")),
    ]

    if any(
        (isinstance(item, Decimal) and item != Decimal("0"))
        or (not isinstance(item, Decimal) and str(item or "").strip())
        for item in optional_amounts
    ):
        values = [
            _dash(optional_amounts[0]),
            _money(optional_amounts[1]),
            _money(optional_amounts[2]),
            _money(optional_amounts[3]),
        ]
        labels = [
            "Benefício Municipal",
            "Cálculo do BM",
            "Total Deduções/Reduções",
            "Desconto Incondicionado",
        ]

        for index, (label, value) in enumerate(zip(labels, values)):
            _draw_field(
                canvas,
                label,
                value,
                BODY_LEFT + COL * index,
                top,
                COL,
                H_063,
            )
        top += H_063

    values = [
        ("BC ISSQN", _money(_value(tax, "base_calculo", Decimal("0")))),
        ("Alíquota Aplicada", _percent(_value(tax, "aliquota", Decimal("0")))),
        ("Retenção do ISSQN", _value(tax, "retencao_issqn")),
        ("ISSQN Apurado", _money(_value(tax, "valor_issqn", Decimal("0")))),
    ]

    for index, (label, value) in enumerate(values):
        _draw_field(
            canvas,
            label,
            value,
            BODY_LEFT + COL * index,
            top,
            COL,
            H_063,
        )

    return top + H_063


def _draw_federal_tax(canvas: Canvas, data: NFSeData, top: float) -> float:
    tax = _value(data, "tributacao_federal", None)

    top = _draw_block_title(canvas, "TRIBUTAÇÃO FEDERAL (EXCETO CBS)", top)

    tp_ret = _value(tax, "codigo_retencao_pis_cofins")

    pis = _value(tax, "pis", Decimal("0"))
    cofins = _value(tax, "cofins", Decimal("0"))
    csll = _value(tax, "csll", Decimal("0"))

    if tp_ret == "1":
        social_retained = csll + pis + cofins
        pis_own = Decimal("0")
        cofins_own = Decimal("0")
    else:
        social_retained = csll
        pis_own = pis
        cofins_own = cofins

    row1 = [
        ("IRRF", _money(_value(tax, "irrf", Decimal("0")))),
        (
            "Contribuição Previdenciária - Retida",
            _money(_value(tax, "contribuicao_previdenciaria", Decimal("0"))),
        ),
        ("Contribuições Sociais - Retidas", _money(social_retained)),
    ]

    widths = [COL, COL, BODY_WIDTH - COL * 2]

    x = BODY_LEFT
    for (label, value), width in zip(row1, widths):
        _draw_field(canvas, label, value, x, top, width, H_063)
        x += width
    top += H_063

    # Nota 6: linha até o fim do ano-calendário de 2026.
    competence_year = ""
    competence = _value(data, "competencia")
    if isinstance(competence, str) and len(competence) >= 4:
        competence_year = competence[-4:]

    if not competence_year or competence_year <= "2026":
        _draw_field(
            canvas,
            "PIS - Débito Apuração Própria",
            _money(pis_own),
            BODY_LEFT,
            top,
            COL,
            H_063,
        )
        _draw_field(
            canvas,
            "COFINS - Débito Apuração Própria",
            _money(cofins_own),
            BODY_LEFT + COL,
            top,
            COL,
            H_063,
        )
        _draw_field(
            canvas,
            "Descrição Contrib. Sociais - Retidas",
            _value(tax, "retencao_pis_cofins"),
            BODY_LEFT + COL * 2,
            top,
            BODY_WIDTH - COL * 2,
            H_063,
            max_chars=35,
        )
        top += H_063

    return top


def _draw_ibs_cbs(canvas: Canvas, data: NFSeData, top: float) -> float:
    tax = (
        _value(data, "tributos_ibs_cbs", None)
        or _value(data, "tributacao_ibs_cbs", None)
    )

    top = _draw_block_title(canvas, "TRIBUTAÇÃO IBS / CBS", top)

    cst_class = " / ".join(
        item
        for item in (
            _value(tax, "cst"),
            _value(tax, "classificacao_tributaria"),
        )
        if item
    )

    incidence = " / ".join(
        item
        for item in (
            _value(tax, "indicador_operacao"),
            _value(tax, "codigo_localidade_incidencia"),
            _value(tax, "localidade_incidencia"),
            _value(tax, "uf_incidencia"),
        )
        if item
    )

    _draw_field(
        canvas,
        "CST / cClassTrib",
        cst_class,
        BODY_LEFT,
        top,
        COL,
        H_063,
        max_chars=12,
    )
    _draw_field(
        canvas,
        "Indicador de Operação / Código IBGE Incidência / Município Incidência / Sigla UF",
        incidence,
        BODY_LEFT + COL,
        top,
        BODY_WIDTH - COL,
        H_063,
        max_chars=56,
    )
    top += H_063

    reductions = " / ".join(
        (
            _percent(_value(tax, "reducao_aliquota_ibs_uf", Decimal("0"))),
            _percent(_value(tax, "reducao_aliquota_ibs_municipal", Decimal("0"))),
            _percent(_value(tax, "reducao_aliquota_cbs", Decimal("0"))),
        )
    )

    ibs_rates = " / ".join(
        (
            _percent(_value(tax, "aliquota_ibs_uf", Decimal("0"))),
            _percent(_value(tax, "aliquota_ibs_municipal", Decimal("0"))),
        )
    )

    row = [
        (
            "Exclusões e Reduções da Base de Cálculo",
            _money(_value(tax, "exclusoes_reducoes_bc", Decimal("0"))),
        ),
        (
            "Base de Cálculo Após Exclusões e Reduções",
            _money(_value(tax, "base_calculo", Decimal("0"))),
        ),
        ("Red. Alíquota IBS / Red. Alíquota CBS", reductions),
        ("Alíquota - IBS UF / IBS Mun", ibs_rates),
    ]

    for index, (label, value) in enumerate(row):
        _draw_field(
            canvas,
            label,
            value,
            BODY_LEFT + COL * index,
            top,
            COL,
            H_063,
        )
    top += H_063

    row = [
        (
            "Alíq. Efetiva Municipal - IBS",
            _percent(_value(tax, "aliquota_efetiva_ibs_municipal", Decimal("0"))),
        ),
        (
            "Valor Apurado Municipal - IBS",
            _money(_value(tax, "valor_ibs_municipal", Decimal("0"))),
        ),
        (
            "Alíq. Efetiva Estadual - IBS",
            _percent(_value(tax, "aliquota_efetiva_ibs_uf", Decimal("0"))),
        ),
        (
            "Valor Apurado Estadual - IBS",
            _money(_value(tax, "valor_ibs_uf", Decimal("0"))),
        ),
    ]

    for index, (label, value) in enumerate(row):
        _draw_field(
            canvas,
            label,
            value,
            BODY_LEFT + COL * index,
            top,
            COL,
            H_063,
        )
    top += H_063

    row = [
        (
            "Valor Total Apurado - IBS",
            _money(_value(tax, "valor_total_ibs", Decimal("0"))),
        ),
        (
            "Alíquota - CBS",
            _percent(_value(tax, "aliquota_cbs", Decimal("0"))),
        ),
        (
            "Alíquota Efetiva - CBS",
            _percent(_value(tax, "aliquota_efetiva_cbs", Decimal("0"))),
        ),
        (
            "Valor Total Apurado - CBS",
            _money(_value(tax, "valor_total_cbs", Decimal("0"))),
        ),
    ]

    for index, (label, value) in enumerate(row):
        _draw_field(
            canvas,
            label,
            value,
            BODY_LEFT + COL * index,
            top,
            COL,
            H_063,
        )

    return top + H_063


def _draw_totals(canvas: Canvas, data: NFSeData, top: float) -> float:
    values = _value(data, "valores", None)
    ibs = (
        _value(data, "tributos_ibs_cbs", None)
        or _value(data, "tributacao_ibs_cbs", None)
    )

    total_ibs_cbs = (
        _value(ibs, "valor_total_ibs", Decimal("0"))
        + _value(ibs, "valor_total_cbs", Decimal("0"))
    )

    total_ret = (
        _value(values, "total_retencoes", Decimal("0"))
        or (
            _value(data, "tributacao_federal", None).total_retencoes
            if _value(data, "tributacao_federal", None)
            else Decimal("0")
        )
    )

    liquid_plus = (
        _value(values, "valor_liquido_com_ibs_cbs", Decimal("0"))
        or (_value(values, "valor_liquido", Decimal("0")) + total_ibs_cbs)
    )

    top = _draw_block_title(canvas, "VALOR TOTAL DA NFS-E", top, height=H_032)

    row = [
        (
            "Valor da Operação / Serviço",
            _money(_value(values, "valor_servico", Decimal("0"))),
        ),
        (
            "Desconto Incondicionado",
            _money(_value(values, "desconto_incondicionado", Decimal("0"))),
        ),
        (
            "Desconto Condicionado",
            _money(_value(values, "desconto_condicionado", Decimal("0"))),
        ),
    ]

    widths = [COL, COL, BODY_WIDTH - COL * 2]
    x = BODY_LEFT

    for (label, value), width in zip(row, widths):
        _draw_field(canvas, label, value, x, top, width, H_067)
        x += width
    top += H_067

    row = [
        ("Total das Retenções (ISSQN / Federais)", _money(total_ret)),
        ("Valor Líquido da NFS-e", _money(_value(values, "valor_liquido", Decimal("0")))),
        ("Total do IBS/CBS", _money(total_ibs_cbs)),
        ("Valor Líquido da NFS-e + IBS/CBS", _money(liquid_plus)),
    ]

    for index, (label, value) in enumerate(row):
        _draw_field(
            canvas,
            label,
            value,
            BODY_LEFT + COL * index,
            top,
            COL,
            H_067,
            shaded=(label == "Valor Líquido da NFS-e + IBS/CBS"),
        )

    return top + H_067


def _approximate_taxes_text(data: NFSeData) -> str:
    values = _value(data, "valores", None)

    federal = _value(values, "tributos_federais_aproximados", Decimal("0"))
    state = _value(values, "tributos_estaduais_aproximados", Decimal("0"))
    municipal = _value(values, "tributos_municipais_aproximados", Decimal("0"))

    return (
        "Totais Aproximados dos Tributos cfe. Lei nº 12.741/2012: "
        f"Federais: {_money(federal)} ; "
        f"Estaduais: {_money(state)} ; "
        f"Municipais: {_money(municipal)}"
    )


def _build_complementary_information(data: NFSeData) -> str:
    pieces: list[str] = []

    info = _value(data, "informacoes_complementares")
    if info:
        pieces.append(f"Inf. Cont.: {info}")

    substitution = _value(data, "substituicao", None)
    substituted_key = (
        _value(substitution, "chave_substituida")
        or _value(data, "chave_substituida")
    )
    if substituted_key:
        pieces.append(f"NFS-e Subst.: {substituted_key}")

    mappings = [
        ("Doc. Ref.", "documento_referencia"),
        ("Cod. Obra", "codigo_obra"),
        ("Insc. Imob.", "inscricao_imobiliaria"),
        ("Cod. Evt.", "codigo_evento"),
        ("Doc. Tec.", "documento_tecnico"),
        ("Núm. Ped.", "numero_pedido"),
        ("Item Ped.", "item_pedido"),
        ("Inf. A. T. Mun.", "informacoes_municipais"),
    ]

    for label, attribute in mappings:
        value = _value(data, attribute)
        if value:
            pieces.append(f"{label}: {value}")

    service = _value(data, "servico", None)
    nbs = _value(service, "codigo_nbs")
    if nbs:
        pieces.append(f"NBS: {nbs}")

    content = " | ".join(pieces)
    tax_line = _approximate_taxes_text(data)

    # A linha de totais aproximados é obrigatória e fixa.
    if content:
        return f"{_truncate(content, 1700)} | {tax_line}"
    return tax_line


def _draw_complementary(canvas: Canvas, data: NFSeData, top: float) -> float:
    top = _draw_block_title(canvas, "INFORMAÇÕES COMPLEMENTARES", top)

    # Altura dinâmica até a área do canhoto ou margem inferior.
    maximum_bottom = 28.00 * cm
    available = max(0.95 * cm, maximum_bottom - top)

    _draw_field(
        canvas,
        "",
        _build_complementary_information(data),
        BODY_LEFT,
        top,
        BODY_WIDTH,
        available,
        max_chars=2000,
        max_lines=max(4, int(available / (0.26 * cm))),
    )

    return top + available


def _draw_stub(canvas: Canvas, data: NFSeData, top: float) -> float:
    top = _draw_block_title(canvas, "CANHOTO", top)

    _draw_field(
        canvas,
        "Data Cientificação",
        "",
        BODY_LEFT,
        top,
        COL,
        H_067,
    )
    _draw_field(
        canvas,
        "Identificação e Assinatura",
        "",
        BODY_LEFT + COL,
        top,
        COL,
        H_067,
    )
    _draw_field(
        canvas,
        "Nº NFS-e / Chave NFS-e",
        f"{_value(data, 'numero_nfse')} / {_value(data, 'chave')}",
        BODY_LEFT + COL * 2,
        top,
        BODY_WIDTH - COL * 2,
        H_067,
        max_chars=66,
    )

    return top + H_067


def _draw_watermark(canvas: Canvas, data: NFSeData) -> None:
    watermark = ""

    if _value(data, "cancelada", False):
        watermark = "CANCELADA"
    elif (
        _value(data, "substituida", False)
        or _value(_value(data, "substituicao", None), "chave_substituida")
    ):
        watermark = "SUBSTITUÍDA"

    if not watermark:
        return

    canvas.saveState()
    canvas.setFillColor(WATERMARK_GRAY)
    canvas.setFont(FONT_ARIAL, WATERMARK_SIZE)
    canvas.translate(PAGE_WIDTH / 2, PAGE_HEIGHT / 2)
    canvas.rotate(45)
    canvas.drawCentredString(0, 0, watermark)
    canvas.restoreState()


# =============================================================================
# Geração pública
# =============================================================================

def generate_danfse_pdf(
    data: NFSeData,
    output_path: str | Path,
    *,
    municipality: MunicipalityInfo | None = None,
    logo_path: str | Path | None = None,
    options: GeneratorOptions | None = None,
) -> Path:
    """
    Gera o DANFSe v2.0 conforme a NT 008 versão 1.02.

    Observações:
    - O arquivo é sempre A4 retrato e de uma única página.
    - Arial e Microsoft Sans Serif são carregadas das fontes do Windows.
    - A logo oficial é carregada automaticamente do arquivo
      logo-nfs-e-horizontal.png na mesma pasta do programa.
    - Outro caminho ainda pode ser informado por logo_path ou
      MunicipalityInfo.logo_path.
    - Campos inexistentes no XML são apresentados com traço (-).
    """
    register_required_fonts()

    municipality = municipality or MunicipalityInfo()
    options = options or GeneratorOptions()

    if options.strict_fonts:
        required = {
            FONT_ARIAL,
            FONT_ARIAL_BOLD,
            FONT_MS_SANS,
            FONT_MS_SANS_BOLD,
        }
        installed = set(pdfmetrics.getRegisteredFontNames())

        if not required.issubset(installed):
            missing = ", ".join(sorted(required - installed))
            raise RuntimeError(
                "Fontes obrigatórias não encontradas no sistema: "
                f"{missing}"
            )

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    canvas = Canvas(
        str(output),
        pagesize=A4,
        pageCompression=1,
    )
    canvas.setTitle(f"DANFSe - DPS {_value(data, 'numero_dps')}")
    canvas.setAuthor("Gerador local DANFSe - NT 008 v1.02")
    canvas.setSubject("Documento Auxiliar da NFS-e")

    # Borda externa de 1 ponto.
    canvas.saveState()
    canvas.setLineWidth(OUTER_LINE_WIDTH)
    canvas.rect(
        PAGE_MARGIN,
        PAGE_MARGIN,
        PAGE_WIDTH - PAGE_MARGIN * 2,
        PAGE_HEIGHT - PAGE_MARGIN * 2,
        stroke=1,
        fill=0,
    )
    canvas.restoreState()

    _draw_header(
        canvas,
        data,
        municipality,
        Path(logo_path) if logo_path else None,
        strict_logo=options.strict_logo,
    )

    top = _draw_identification(canvas, data)

    top = _draw_person_block(
        canvas,
        "PRESTADOR / FORNECEDOR",
        _value(data, "prestador"),
        top,
        include_sn=True,
        simples_nacional=_value(data, "simples_nacional"),
        regime_apuracao=_value(data, "regime_apuracao_sn"),
        suppress_optional_lines=options.suppress_optional_address_lines,
    )

    tomador = _value(data, "tomador", None)
    if _person_is_present(tomador):
        top = _draw_person_block(
            canvas,
            "TOMADOR / ADQUIRENTE",
            tomador,
            top,
            suppress_optional_lines=options.suppress_optional_address_lines,
        )
    else:
        top = _draw_message_block(
            canvas,
            "TOMADOR/ADQUIRENTE DA OPERAÇÃO NÃO IDENTIFICADO NA NFS-e",
            top,
        )

    destinatario = _value(data, "destinatario", None)

    if _person_same(destinatario, tomador):
        top = _draw_message_block(
            canvas,
            "O DESTINATÁRIO É O PRÓPRIO TOMADOR/ADQUIRENTE DA OPERAÇÃO",
            top,
        )
    elif _person_is_present(destinatario):
        top = _draw_person_block(
            canvas,
            "DESTINATÁRIO DA OPERAÇÃO",
            destinatario,
            top,
            suppress_optional_lines=options.suppress_optional_address_lines,
        )
    else:
        top = _draw_message_block(
            canvas,
            "DESTINATÁRIO DA OPERAÇÃO NÃO IDENTIFICADO NA NFS-e",
            top,
        )

    intermediario = _value(data, "intermediario", None)
    if _person_is_present(intermediario):
        top = _draw_person_block(
            canvas,
            "INTERMEDIÁRIO DA OPERAÇÃO",
            intermediario,
            top,
            suppress_optional_lines=options.suppress_optional_address_lines,
        )
    else:
        top = _draw_message_block(
            canvas,
            "INTERMEDIÁRIO DA OPERAÇÃO NÃO IDENTIFICADO NA NFS-e",
            top,
        )

    top = _draw_service(canvas, data, top)
    top = _draw_municipal_tax(canvas, data, top)
    top = _draw_federal_tax(canvas, data, top)
    top = _draw_ibs_cbs(canvas, data, top)
    top = _draw_totals(canvas, data, top)

    # Se o canhoto estiver ativo, reserva espaço para ele.
    if options.include_stub:
        complementary_bottom = 27.38 * cm
    else:
        complementary_bottom = 28.85 * cm

    top = _draw_block_title(canvas, "INFORMAÇÕES COMPLEMENTARES", top)

    available = max(0.55 * cm, complementary_bottom - top)

    _draw_field(
        canvas,
        "",
        _build_complementary_information(data),
        BODY_LEFT,
        top,
        BODY_WIDTH,
        available,
        max_chars=2000,
        max_lines=max(3, int(available / (0.24 * cm))),
    )
    top += available

    if options.include_stub:
        top = _draw_stub(canvas, data, top)

    if options.strict_one_page and top > 29.45 * cm:
        raise ValueError(
            "O conteúdo excedeu a única página permitida pela NT 008. "
            "Considere suprimir linhas opcionais de endereço/e-mail ou o canhoto."
        )

    _draw_watermark(canvas, data)

    canvas.showPage()
    canvas.save()

    return output


def generate_from_xml(
    xml_path: str | Path,
    output_path: str | Path | None = None,
    *,
    municipality: MunicipalityInfo | None = None,
    municipality_resolver: Callable[[str], tuple[str, str] | None] | None = None,
    logo_path: str | Path | None = None,
    options: GeneratorOptions | None = None,
) -> Path:
    data = parse_nfse_file(
        xml_path,
        municipality_resolver=municipality_resolver,
    )

    standard_name = data.nome_pdf()

    if output_path is None:
        output_path = Path(xml_path).with_name(standard_name)
    else:
        requested = Path(output_path)

        # Se foi informado um arquivo, usa apenas a pasta e mantém o nome
        # oficial DPS_<numero>.pdf. Se foi informada uma pasta, salva nela.
        if requested.suffix.lower() == ".pdf":
            output_path = requested.parent / standard_name
        else:
            output_path = requested / standard_name

    return generate_danfse_pdf(
        data,
        output_path,
        municipality=municipality,
        logo_path=logo_path,
        options=options,
    )


__all__ = [
    "MunicipalityInfo",
    "GeneratorOptions",
    "generate_danfse_pdf",
    "generate_from_xml",
    "register_required_fonts",
    "GENERATOR_BUILD",
]
