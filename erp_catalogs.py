"""Catálogos operacionais compartilhados pelo Suprimentos e pelo MES.

As transformações são consultadas no Cadastro a cada solicitação. Os demais
catálogos operacionais são compartilhados por API entre Suprimentos e MES.
"""

from sqlalchemy import text

NOT_APPLICABLE = "N/A"

# N/A é uma escolha explícita do usuário, distinta de um campo não
# preenchido. Ela permite publicar uma O.S. sem inventar uma informação que
# não se aplica àquela entrada (pós-venda, acessório, retorno etc.).
VENDEDORES = ["JI", "JOÃO CARVALHO", "SERGIO BRUNETTI", "OUTROS", NOT_APPLICABLE]
MERCADOS = ["LICITAÇÃO", "VAREJO", NOT_APPLICABLE]
TIPOS_VEICULO = ["MICRO", "MISTO", NOT_APPLICABLE]
LINHAS = ["LB", "LAB", "LE", "LAE", NOT_APPLICABLE]

AR_FORNECEDORES = ["GE", "CLIM", NOT_APPLICABLE]
AR_TIPOS = [
    "COMPLEMENTO",
    "CABINE/SALÃO",
    "COMPLEMENTO C/ CX CONDENSADORA",
    "DERIVAÇÃO C/ CX PRÉ CONDENSADORA",
    "COMPLEMENTO E/S/J C/ COMANDO TRASEIRO BOTÃO",
    "COMPLEMENTO E/S/J C/ COMANDO ORIGINAL",
    "COMPLEMENTO E/S/J COM COMANDO TRASEIRO DIGITAL",
    "SEGUNDO COMPRESSOR COM CONDENSADOR DE TETO COM DOIS ELETROS SALAO SPRINTER ANTIBACTERICIDA",
    "SEGUNDO COMPRESSOR COM CONDENSADOR DE TETO AR QUENTE E CAIXA ANTIBACTERICIDA",
    "OUTRO",
    "AG",
    "AR ORIGINAL",
    "NÃO",
    "COMPLEMENTO C/ COMANDO DIGITAL TRASEIRO",
    NOT_APPLICABLE,
]
SIM_NAO = ["SIM", "NÃO", NOT_APPLICABLE]

MARCAS = [
    "MERCEDES-BENZ", "FORD", "RENAULT", "PEUGEOT", "CITROËN", "FIAT", "IVECO",
]
MODELOS = [
    "SPRINTER 417", "SPRINTER 516", "SPRINTER 517", "TRANSIT", "MASTER", "EXPERT", "BOXER",
    "JUMPY", "JUMPER", "SCUDO", "DUCATO", "DAILY 45-160", "DAILY 50-180",
    "KANGOO",
]
VERSOES = [
    "9,5 M³ FURGÃO",
    "19,5 M³ 16L",
    "9,5 M³ VITRÊ REVESTIDA",
    "10,5 M³",
    "14 M³",
    "15,5 M³",
    "10,5 M³ VITRÊ REVESTIDA",
    "14 M³ VITRÊ REVESTIDA",
    "15,5 M³ VITRÊ REVESTIDA",
    "10,5 M³ ORIGINAL 16L",
    "15,5 M³ ORIGINAL 18L",
    "15,5 M³ ORIGINAL 20L",
    "15,5 M³ ORIGINAL 21L",
    "L2H3",
    "L3H3",
    "L3H2 VITRÊ",
    "L4H3 VITRÊ",
    "L3H2 ORIGINAL 15L",
    "L4H3 ORIGINAL 18L",
    "L1H1",
    "L2H2",
    "L3H2",
    "L3H2 ORIGINAL 16L",
    "FURGÃO",
    "VITRÊ",
    "ORIGINAL 17L",
    "ORIGINAL 16L",
]

def transformation_options(conn=None):
    """Read active group-40 registrations, including renamed/new transformations.

    The SKU prefix is the canonical structural group in Cadastro. Reading the
    registrations directly avoids relying on the Estoque mirror or a code list
    that needs a deployment for each new product. N/A remains an explicit choice.
    """
    options = [{"codigo": NOT_APPLICABLE, "descricao": NOT_APPLICABLE}]
    if conn is None:
        return options
    rows = conn.execute(text("""
        select sku, descricao_primaria
          from cadastro_registros
         where sku like '40%' and ativo is not false
         order by sku
    """)).mappings()
    for row in rows:
        code = str(row["sku"] or "").strip()
        description = str(row["descricao_primaria"] or "").strip()
        if len(code) == 8 and code.isdigit() and description:
            options.append({"codigo": code, "descricao": description})
    return options


# Only the service type is structural.  The operational O.S. can be opened and
# activated with the remaining commercial data blank or explicitly marked N/A;
# the stage parametrization remains the activation safety gate.
REQUIRED_WORK_ORDER_FIELDS = ["tipo_servico"]


def payload(conn=None):
    return {
        "vendedores": VENDEDORES,
        "mercados": MERCADOS,
        "tipos_veiculo": TIPOS_VEICULO,
        "linhas": LINHAS,
        "ar_fornecedores": AR_FORNECEDORES,
        "ar_tipos": AR_TIPOS,
        "sim_nao": SIM_NAO,
        "marcas": MARCAS,
        "modelos": MODELOS,
        "versoes": VERSOES,
        "transformacoes": transformation_options(conn),
        "required_work_order_fields": REQUIRED_WORK_ORDER_FIELDS,
    }
