from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable
from xml.etree import ElementTree as ET


# =============================================================================
# Tipos de dados
# =============================================================================

@dataclass(slots=True)
class Endereco:
    logradouro: str = ""
    numero: str = ""
    complemento: str = ""
    bairro: str = ""
    codigo_municipio: str = ""
    municipio: str = ""
    uf: str = ""
    cep: str = ""
    codigo_pais: str = ""
    pais: str = ""

    @property
    def linha(self) -> str:
        return ", ".join(
            parte
            for parte in (
                self.logradouro,
                self.numero,
                self.complemento,
                self.bairro,
            )
            if parte
        )

    @property
    def municipio_uf(self) -> str:
        if self.municipio and self.uf:
            return f"{self.municipio} - {self.uf}"
        return self.municipio or self.uf


@dataclass(slots=True)
class Pessoa:
    documento: str = ""
    tipo_documento: str = ""
    inscricao_municipal: str = ""
    nome: str = ""
    telefone: str = ""
    email: str = ""
    endereco: Endereco = field(default_factory=Endereco)


@dataclass(slots=True)
class Servico:
    codigo_tributacao_nacional: str = ""
    descricao_tributacao_nacional: str = ""
    codigo_tributacao_municipal: str = ""
    descricao_tributacao_municipal: str = ""
    codigo_nbs: str = ""
    descricao_nbs: str = ""
    descricao: str = ""
    codigo_local_prestacao: str = ""
    local_prestacao: str = ""
    uf_prestacao: str = ""
    pais_prestacao: str = ""


@dataclass(slots=True)
class TributacaoMunicipal:
    codigo_tributacao_issqn: str = ""
    tributacao_issqn: str = ""
    codigo_municipio_incidencia: str = ""
    municipio_incidencia: str = ""
    uf_incidencia: str = ""
    pais_resultado: str = ""
    codigo_regime_especial: str = ""
    regime_especial: str = ""
    codigo_imunidade: str = ""
    imunidade: str = ""
    codigo_suspensao: str = ""
    suspensao: str = ""
    numero_processo_suspensao: str = ""
    beneficio_municipal: str = ""
    valor_servico: Decimal = Decimal("0")
    desconto_incondicionado: Decimal = Decimal("0")
    total_deducoes_reducoes: Decimal = Decimal("0")
    calculo_beneficio_municipal: Decimal = Decimal("0")
    base_calculo: Decimal = Decimal("0")
    aliquota: Decimal = Decimal("0")
    codigo_retencao_issqn: str = ""
    retencao_issqn: str = ""
    valor_issqn: Decimal = Decimal("0")


@dataclass(slots=True)
class TributacaoFederal:
    irrf: Decimal = Decimal("0")
    contribuicao_previdenciaria: Decimal = Decimal("0")
    csll: Decimal = Decimal("0")
    pis: Decimal = Decimal("0")
    cofins: Decimal = Decimal("0")
    codigo_retencao_pis_cofins: str = ""
    retencao_pis_cofins: str = ""

    @property
    def total_retencoes(self) -> Decimal:
        return (
            self.irrf
            + self.contribuicao_previdenciaria
            + self.csll
            + self.pis
            + self.cofins
        )


@dataclass(slots=True)
class Valores:
    valor_servico: Decimal = Decimal("0")
    desconto_condicionado: Decimal = Decimal("0")
    desconto_incondicionado: Decimal = Decimal("0")
    issqn_retido: Decimal = Decimal("0")
    total_retencoes_federais: Decimal = Decimal("0")
    pis_cofins_debito_proprio: Decimal = Decimal("0")
    valor_liquido: Decimal = Decimal("0")
    tributos_federais_aproximados: Decimal = Decimal("0")
    tributos_estaduais_aproximados: Decimal = Decimal("0")
    tributos_municipais_aproximados: Decimal = Decimal("0")


@dataclass(slots=True)
class NFSeData:
    chave: str = ""
    numero_nfse: str = ""
    competencia: str = ""
    emissao_nfse: str = ""
    numero_dps: str = ""
    serie_dps: str = ""
    emissao_dps: str = ""

    codigo_ambiente: str = ""
    ambiente: str = ""
    ambiente_gerador: str = ""
    codigo_emitente: str = ""
    emitente_nfse: str = ""
    codigo_situacao: str = ""
    situacao: str = ""

    municipio_emitente: str = ""
    uf_emitente: str = ""
    telefone_prefeitura: str = ""
    email_prefeitura: str = ""

    prestador: Pessoa = field(default_factory=Pessoa)
    tomador: Pessoa | None = None
    intermediario: Pessoa | None = None

    simples_nacional: str = ""
    regime_apuracao_sn: str = ""

    servico: Servico = field(default_factory=Servico)
    tributacao_municipal: TributacaoMunicipal = field(
        default_factory=TributacaoMunicipal
    )
    tributacao_federal: TributacaoFederal = field(
        default_factory=TributacaoFederal
    )
    valores: Valores = field(default_factory=Valores)

    informacoes_complementares: str = ""
    versao_xml: str = ""

    @property
    def homologacao(self) -> bool:
        return self.codigo_ambiente == "2"

    @property
    def cancelada(self) -> bool:
        return self.codigo_situacao not in {"", "100"}

    def nome_pdf(self) -> str:
        # Mantém o padrão observado no DANFSe nacional: DPS_30774.pdf
        numero = safe_filename(self.numero_dps or self.numero_nfse or self.chave)
        return f"DPS_{numero}.pdf"


# =============================================================================
# Traduções dos códigos do leiaute
# =============================================================================

AMBIENTES = {
    "1": "Produção",
    "2": "Homologação",
}

TIPOS_EMITENTE = {
    "1": "Prestador do Serviço",
    "2": "Tomador do Serviço",
    "3": "Intermediário do Serviço",
}

SITUACOES = {
    "100": "NFS-e Gerada",
    "101": "NFS-e Cancelada",
}

SIMPLES_NACIONAL = {
    "1": "Não optante",
    "2": "Optante - Microempreendedor Individual (MEI)",
    "3": "Optante - Microempresa ou Empresa de Pequeno Porte",
}

REGIME_APURACAO_SN = {
    "1": "Regime de apuração dos tributos federais e municipal pelo SN",
    "2": "Regime de apuração dos tributos federais pelo SN e ISSQN por fora",
    "3": "Regime de apuração dos tributos federais e ISSQN por fora do SN",
}

REGIMES_ESPECIAIS = {
    "0": "Nenhum",
    "1": "Microempresa municipal",
    "2": "Estimativa",
    "3": "Sociedade de profissionais",
    "4": "Cooperativa",
    "5": "Microempreendedor Individual (MEI)",
    "6": "Microempresa ou Empresa de Pequeno Porte (ME/EPP)",
    "9": "Não informado",
}

TRIBUTACAO_ISSQN = {
    "1": "Operação Tributável",
    "2": "Imunidade",
    "3": "Exportação de Serviço",
    "4": "Não incidência",
}

RETENCAO_ISSQN = {
    "1": "Não Retido",
    "2": "Retido pelo Tomador",
    "3": "Retido pelo Intermediário",
}

RETENCAO_PIS_COFINS = {
    "1": "PIS/COFINS Retido",
    "2": "PIS/COFINS Não Retido",
}


# =============================================================================
# Utilidades XML
# =============================================================================

def local_name(tag: str) -> str:
    return tag.split("}", 1)[-1].split(":")[-1]


def child(element: ET.Element | None, name: str) -> ET.Element | None:
    if element is None:
        return None

    for item in list(element):
        if local_name(item.tag) == name:
            return item

    return None


def descendant(element: ET.Element | None, name: str) -> ET.Element | None:
    if element is None:
        return None

    for item in element.iter():
        if local_name(item.tag) == name:
            return item

    return None


def path(element: ET.Element | None, *names: str) -> ET.Element | None:
    current = element

    for name in names:
        current = child(current, name)
        if current is None:
            return None

    return current


def text(element: ET.Element | None, name: str, default: str = "") -> str:
    item = child(element, name)

    if item is None:
        return default

    value = (item.text or "").strip()
    return value or default


def descendant_text(
    element: ET.Element | None,
    name: str,
    default: str = "",
) -> str:
    item = descendant(element, name)

    if item is None:
        return default

    value = (item.text or "").strip()
    return value or default


def first_text(
    element: ET.Element | None,
    *names: str,
    default: str = "",
) -> str:
    for name in names:
        value = descendant_text(element, name)
        if value:
            return value

    return default


def only_digits(value: str) -> str:
    return "".join(character for character in value if character.isdigit())


def decimal_value(value: str | None) -> Decimal:
    if not value:
        return Decimal("0")

    normalized = value.strip().replace(" ", "")

    if "," in normalized and "." in normalized:
        normalized = normalized.replace(".", "").replace(",", ".")
    else:
        normalized = normalized.replace(",", ".")

    try:
        return Decimal(normalized)
    except InvalidOperation:
        return Decimal("0")


def safe_filename(value: str) -> str:
    invalid = '<>:"/\\|?*'
    result = "".join("_" if char in invalid else char for char in value)
    return result.strip().strip(".")[:120] or "SEM_NUMERO"


def format_datetime(value: str) -> str:
    if not value:
        return ""

    normalized = value.strip().replace("Z", "+00:00")

    try:
        parsed = datetime.fromisoformat(normalized)

        if "T" in value:
            return parsed.strftime("%d/%m/%Y %H:%M:%S")

        return parsed.strftime("%d/%m/%Y")

    except ValueError:
        return value


def format_document(value: str) -> tuple[str, str]:
    digits = only_digits(value)

    if len(digits) == 11:
        return (
            f"{digits[:3]}.{digits[3:6]}.{digits[6:9]}-{digits[9:]}",
            "CPF",
        )

    if len(digits) == 14:
        return (
            f"{digits[:2]}.{digits[2:5]}.{digits[5:8]}/"
            f"{digits[8:12]}-{digits[12:]}",
            "CNPJ",
        )

    if value:
        return value, "NIF"

    return "", ""


def format_phone(value: str) -> str:
    digits = only_digits(value)

    if len(digits) == 10:
        return f"({digits[:2]}) {digits[2:6]}-{digits[6:]}"

    if len(digits) == 11:
        return f"({digits[:2]}) {digits[2:7]}-{digits[7:]}"

    return value


def format_cep(value: str) -> str:
    digits = only_digits(value)

    if len(digits) == 8:
        return f"{digits[:5]}-{digits[5:]}"

    return value


def format_tributation_code(value: str) -> str:
    digits = only_digits(value)

    if len(digits) == 6:
        return f"{digits[:2]}.{digits[2:4]}.{digits[4:]}"

    return value


def format_nbs(value: str) -> str:
    """Formata o NBS no padrão n.nnnn.nn.nn quando possuir 9 dígitos."""
    digits = only_digits(value)

    if len(digits) == 9:
        return f"{digits[0]}.{digits[1:5]}.{digits[5:7]}.{digits[7:9]}"

    return value


# =============================================================================
# Parsers de pessoas e endereços
# =============================================================================

def parse_address(
    parent: ET.Element | None,
    municipality_resolver: Callable[[str], tuple[str, str] | None] | None,
) -> Endereco:
    if parent is None:
        return Endereco()

    # Emitente: <enderNac> diretamente.
    address = child(parent, "enderNac")

    # Tomador/intermediário: <end><endNac>...</endNac><xLgr>...</end>
    end_container = child(parent, "end")

    if address is None and end_container is not None:
        address = child(end_container, "endNac") or child(
            end_container,
            "endExt",
        )

    source = address or end_container or parent

    codigo_municipio = first_text(source, "cMun")
    municipio = first_text(source, "xMun", "xCidade", "xLoc")
    uf = first_text(source, "UF")

    if codigo_municipio and municipality_resolver is not None:
        resolved = municipality_resolver(codigo_municipio)

        if resolved:
            municipio = municipio or resolved[0]
            uf = uf or resolved[1]

    # No bloco do tomador, logradouro/número ficam em <end>, e não em <endNac>.
    street_source = end_container or source

    return Endereco(
        logradouro=first_text(street_source, "xLgr"),
        numero=first_text(street_source, "nro"),
        complemento=first_text(street_source, "xCpl"),
        bairro=first_text(street_source, "xBairro"),
        codigo_municipio=codigo_municipio,
        municipio=municipio,
        uf=uf,
        cep=format_cep(first_text(source, "CEP", "cEndPost")),
        codigo_pais=first_text(source, "cPais"),
        pais=first_text(source, "xPais"),
    )


def parse_person(
    parent: ET.Element | None,
    municipality_resolver: Callable[[str], tuple[str, str] | None] | None,
) -> Pessoa | None:
    if parent is None:
        return None

    raw_document = first_text(parent, "CNPJ", "CPF", "NIF")
    document, document_type = format_document(raw_document)

    person = Pessoa(
        documento=document,
        tipo_documento=document_type,
        inscricao_municipal=first_text(parent, "IM"),
        nome=first_text(parent, "xNome"),
        telefone=format_phone(first_text(parent, "fone")),
        email=first_text(parent, "email"),
        endereco=parse_address(parent, municipality_resolver),
    )

    if not any(
        (
            person.documento,
            person.nome,
            person.inscricao_municipal,
            person.telefone,
            person.email,
            person.endereco.logradouro,
        )
    ):
        return None

    return person


def merge_people(primary: Pessoa | None, secondary: Pessoa | None) -> Pessoa:
    primary = primary or Pessoa()
    secondary = secondary or Pessoa()

    return Pessoa(
        documento=primary.documento or secondary.documento,
        tipo_documento=primary.tipo_documento or secondary.tipo_documento,
        inscricao_municipal=(
            primary.inscricao_municipal
            or secondary.inscricao_municipal
        ),
        nome=primary.nome or secondary.nome,
        telefone=primary.telefone or secondary.telefone,
        email=primary.email or secondary.email,
        endereco=Endereco(
            logradouro=(
                primary.endereco.logradouro
                or secondary.endereco.logradouro
            ),
            numero=primary.endereco.numero or secondary.endereco.numero,
            complemento=(
                primary.endereco.complemento
                or secondary.endereco.complemento
            ),
            bairro=primary.endereco.bairro or secondary.endereco.bairro,
            codigo_municipio=(
                primary.endereco.codigo_municipio
                or secondary.endereco.codigo_municipio
            ),
            municipio=(
                primary.endereco.municipio
                or secondary.endereco.municipio
            ),
            uf=primary.endereco.uf or secondary.endereco.uf,
            cep=primary.endereco.cep or secondary.endereco.cep,
            codigo_pais=(
                primary.endereco.codigo_pais
                or secondary.endereco.codigo_pais
            ),
            pais=primary.endereco.pais or secondary.endereco.pais,
        ),
    )


# =============================================================================
# Parser principal
# =============================================================================

def parse_nfse(
    xml_source: bytes | str | Path,
    *,
    municipality_resolver: Callable[[str], tuple[str, str] | None] | None = None,
    validate: bool = True,
) -> NFSeData:
    if isinstance(xml_source, Path):
        xml_bytes = xml_source.read_bytes()

    elif isinstance(xml_source, str):
        possible_path = Path(xml_source)

        if possible_path.exists():
            xml_bytes = possible_path.read_bytes()
        else:
            xml_bytes = xml_source.encode("utf-8")

    else:
        xml_bytes = xml_source

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise ValueError(f"XML inválido: {exc}") from exc

    inf_nfse = descendant(root, "infNFSe")

    if inf_nfse is None:
        raise ValueError("Bloco infNFSe não encontrado.")

    dps = child(inf_nfse, "DPS") or descendant(inf_nfse, "DPS")
    inf_dps = descendant(dps, "infDPS")

    if inf_dps is None:
        raise ValueError("Bloco DPS/infDPS não encontrado.")

    emit = child(inf_nfse, "emit")
    prest = child(inf_dps, "prest")
    toma = child(inf_dps, "toma")
    interm = child(inf_dps, "interm")

    # A DPS possui dados fiscais do prestador, mas o nome/endereço completos
    # estão no bloco <emit> da NFS-e.
    prestador = merge_people(
        parse_person(prest, municipality_resolver),
        parse_person(emit, municipality_resolver),
    )

    tomador = parse_person(toma, municipality_resolver)
    intermediario = parse_person(interm, municipality_resolver)

    raw_id = inf_nfse.attrib.get("Id", "")
    chave = only_digits(
        raw_id[3:] if raw_id.upper().startswith("NFS") else raw_id
    )

    codigo_ambiente = text(inf_dps, "tpAmb")
    codigo_emitente = text(inf_dps, "tpEmit")
    codigo_situacao = text(inf_nfse, "cStat")

    reg_trib = path(prest, "regTrib")
    op_simples = text(reg_trib, "opSimpNac")
    regime_apuracao = text(reg_trib, "regApTribSN")
    regime_especial_code = text(reg_trib, "regEspTrib")

    service = child(inf_dps, "serv")
    location = child(service, "locPrest")
    service_code = child(service, "cServ")
    additional_info = child(service, "infoCompl")

    codigo_local_prestacao = text(location, "cLocPrestacao")
    local_prestacao = text(inf_nfse, "xLocPrestacao")
    uf_prestacao = ""

    if codigo_local_prestacao and municipality_resolver:
        resolved = municipality_resolver(codigo_local_prestacao)
        if resolved:
            local_prestacao = local_prestacao or resolved[0]
            uf_prestacao = resolved[1]

    valores_dps = child(inf_dps, "valores")
    serv_prest = child(valores_dps, "vServPrest")
    trib = child(valores_dps, "trib")
    trib_mun = child(trib, "tribMun")
    trib_fed = child(trib, "tribFed")
    pis_cofins = child(trib_fed, "piscofins")
    total_tributos = path(trib, "totTrib", "vTotTrib")

    valores_nfse = child(inf_nfse, "valores")

    valor_servico = decimal_value(text(serv_prest, "vServ"))
    valor_issqn = decimal_value(text(valores_nfse, "vISSQN"))
    tipo_retencao_issqn = text(trib_mun, "tpRetISSQN")

    federal = TributacaoFederal(
        irrf=decimal_value(text(trib_fed, "vRetIRRF")),
        contribuicao_previdenciaria=decimal_value(
            text(trib_fed, "vRetCP")
        ),
        csll=decimal_value(text(trib_fed, "vRetCSLL")),
        pis=decimal_value(text(pis_cofins, "vPis")),
        cofins=decimal_value(text(pis_cofins, "vCofins")),
        codigo_retencao_pis_cofins=text(
            pis_cofins,
            "tpRetPisCofins",
        ),
        retencao_pis_cofins=RETENCAO_PIS_COFINS.get(
            text(pis_cofins, "tpRetPisCofins"),
            "",
        ),
    )

    resultado = NFSeData(
        chave=chave,
        numero_nfse=text(inf_nfse, "nNFSe"),
        competencia=format_datetime(text(inf_dps, "dCompet")),
        emissao_nfse=format_datetime(text(inf_nfse, "dhProc")),
        numero_dps=text(inf_dps, "nDPS"),
        serie_dps=text(inf_dps, "serie"),
        emissao_dps=format_datetime(text(inf_dps, "dhEmi")),
        codigo_ambiente=codigo_ambiente,
        ambiente=AMBIENTES.get(codigo_ambiente, codigo_ambiente),
        ambiente_gerador=text(inf_nfse, "ambGer"),
        codigo_emitente=codigo_emitente,
        emitente_nfse=TIPOS_EMITENTE.get(
            codigo_emitente,
            codigo_emitente,
        ),
        codigo_situacao=codigo_situacao,
        situacao=SITUACOES.get(codigo_situacao, codigo_situacao),
        municipio_emitente=text(inf_nfse, "xLocEmi"),
        uf_emitente=prestador.endereco.uf,
        prestador=prestador,
        tomador=tomador,
        intermediario=intermediario,
        simples_nacional=SIMPLES_NACIONAL.get(
            op_simples,
            op_simples,
        ),
        regime_apuracao_sn=REGIME_APURACAO_SN.get(
            regime_apuracao,
            "-",
        ),
        servico=Servico(
            codigo_tributacao_nacional=format_tributation_code(
                text(service_code, "cTribNac")
            ),
            descricao_tributacao_nacional=text(inf_nfse, "xTribNac"),
            codigo_tributacao_municipal=text(inf_nfse, "cTribMun"),
            descricao_tributacao_municipal=text(
                inf_nfse,
                "xTribMun",
            ),
            codigo_nbs=format_nbs(text(service_code, "cNBS")),
            descricao_nbs=text(inf_nfse, "xNBS"),
            descricao=text(service_code, "xDescServ"),
            codigo_local_prestacao=codigo_local_prestacao,
            local_prestacao=local_prestacao,
            uf_prestacao=uf_prestacao,
            pais_prestacao=(
                text(location, "cPaisPrestacao")
                or text(location, "cPais")
                or "BR"
            ),
        ),
        tributacao_municipal=TributacaoMunicipal(
            codigo_tributacao_issqn=text(trib_mun, "tribISSQN"),
            tributacao_issqn=TRIBUTACAO_ISSQN.get(
                text(trib_mun, "tribISSQN"),
                text(trib_mun, "tribISSQN"),
            ),
            codigo_municipio_incidencia=text(
                inf_nfse,
                "cLocIncid",
            ),
            municipio_incidencia=text(inf_nfse, "xLocIncid"),
            uf_incidencia=prestador.endereco.uf,
            pais_resultado=(
                text(trib_mun, "cPaisResult")
                or text(trib_mun, "cPais")
                or "BR"
            ),
            codigo_regime_especial=regime_especial_code,
            regime_especial=REGIMES_ESPECIAIS.get(
                regime_especial_code,
                regime_especial_code,
            ),
            codigo_imunidade=text(trib_mun, "tpImunidade"),
            imunidade=text(trib_mun, "tpImunidade"),
            codigo_suspensao=descendant_text(
                trib_mun,
                "tpSusp",
            ),
            suspensao=(
                "Sim"
                if descendant_text(trib_mun, "tpSusp")
                else "Não"
            ),
            numero_processo_suspensao=descendant_text(
                trib_mun,
                "nProcesso",
            ),
            beneficio_municipal=text(valores_nfse, "tpBM"),
            valor_servico=valor_servico,
            desconto_incondicionado=decimal_value(
                descendant_text(valores_dps, "vDescIncond")
            ),
            total_deducoes_reducoes=decimal_value(
                first_text(valores_dps, "vDedRed", "vDR")
            ),
            calculo_beneficio_municipal=decimal_value(
                first_text(valores_nfse, "vCalcBM", "vRedBCBM")
            ),
            base_calculo=decimal_value(text(valores_nfse, "vBC")),
            aliquota=decimal_value(text(valores_nfse, "pAliqAplic")),
            codigo_retencao_issqn=tipo_retencao_issqn,
            retencao_issqn=RETENCAO_ISSQN.get(
                tipo_retencao_issqn,
                tipo_retencao_issqn,
            ),
            valor_issqn=valor_issqn,
        ),
        tributacao_federal=federal,
        valores=Valores(
            valor_servico=valor_servico,
            desconto_condicionado=decimal_value(
                descendant_text(valores_dps, "vDescCond")
            ),
            desconto_incondicionado=decimal_value(
                descendant_text(valores_dps, "vDescIncond")
            ),
            issqn_retido=(
                valor_issqn
                if tipo_retencao_issqn in {"2", "3"}
                else Decimal("0")
            ),
            total_retencoes_federais=federal.total_retencoes,
            pis_cofins_debito_proprio=federal.pis + federal.cofins,
            valor_liquido=decimal_value(text(valores_nfse, "vLiq")),
            tributos_federais_aproximados=decimal_value(
                text(total_tributos, "vTotTribFed")
            ),
            tributos_estaduais_aproximados=decimal_value(
                text(total_tributos, "vTotTribEst")
            ),
            tributos_municipais_aproximados=decimal_value(
                text(total_tributos, "vTotTribMun")
            ),
        ),
        informacoes_complementares=text(
            additional_info,
            "xInfComp",
        ),
        versao_xml=root.attrib.get("versao", ""),
    )

    if validate:
        validate_nfse(resultado)

    return resultado


def parse_nfse_file(
    filename: str | Path,
    *,
    municipality_resolver: Callable[[str], tuple[str, str] | None] | None = None,
    validate: bool = True,
) -> NFSeData:
    return parse_nfse(
        Path(filename),
        municipality_resolver=municipality_resolver,
        validate=validate,
    )


def validate_nfse(data: NFSeData) -> None:
    errors: list[str] = []

    if not data.chave:
        errors.append("Chave de acesso não encontrada.")
    elif len(data.chave) != 50:
        errors.append(
            f"Chave com {len(data.chave)} dígitos; esperado: 50."
        )

    if not data.numero_nfse:
        errors.append("Número da NFS-e não encontrado.")

    if not data.numero_dps:
        errors.append("Número da DPS não encontrado.")

    if not data.serie_dps:
        errors.append("Série da DPS não encontrada.")

    if not data.prestador.nome:
        errors.append("Nome do prestador não encontrado.")

    if not data.prestador.documento:
        errors.append("Documento do prestador não encontrado.")

    if not data.servico.descricao:
        errors.append("Descrição do serviço não encontrada.")

    if errors:
        raise ValueError(" | ".join(errors))


def decimal_to_br(value: Decimal, places: int = 2) -> str:
    quantizer = Decimal("1").scaleb(-places)
    formatted = f"{value.quantize(quantizer):,.{places}f}"

    return (
        formatted
        .replace(",", "#")
        .replace(".", ",")
        .replace("#", ".")
    )


def money_br(value: Decimal, empty_zero: bool = False) -> str:
    if empty_zero and value == Decimal("0"):
        return "-"

    return f"R$ {decimal_to_br(value)}"


def percent_br(value: Decimal, empty_zero: bool = False) -> str:
    if empty_zero and value == Decimal("0"):
        return "-"

    return f"{decimal_to_br(value)} %"


__all__ = [
    "Endereco",
    "Pessoa",
    "Servico",
    "TributacaoMunicipal",
    "TributacaoFederal",
    "Valores",
    "NFSeData",
    "parse_nfse",
    "parse_nfse_file",
    "validate_nfse",
    "decimal_to_br",
    "money_br",
    "percent_br",
]
