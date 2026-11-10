from pathlib import Path
import sys

from danfse_generator import MunicipalityInfo, generate_from_xml


def resolve(code: str):
    mapping = {
        "4209102": ("Joinville", "SC"),
        "4208450": ("Itapoá", "SC"),
    }
    return mapping.get(code)


if len(sys.argv) < 2:
    print("Uso: python validar_xml.py caminho_do_xml")
    raise SystemExit(2)

xml = Path(sys.argv[1])
result = generate_from_xml(
    xml,
    municipality=MunicipalityInfo(),
    municipality_resolver=resolve,
)

print(f"PDF gerado: {result}")
