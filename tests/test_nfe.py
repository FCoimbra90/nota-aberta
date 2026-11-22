"""NF-e modelo 55 de qualquer empresa: XML que a SEFAZ aceita, assinatura conferida por C14N independente, e o emissor
com as salvaguardas contra nota dupla e número queimado (SEFAZ falsa; a real foi medida na homologação, ver OPERACAO.md)."""
from __future__ import annotations

import base64
import copy
import datetime as dt
import hashlib
import sys
from decimal import Decimal as D
from pathlib import Path
from typing import ClassVar

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import pkcs12

lxml = pytest.importorskip("lxml.etree")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_contas import CNPJ, MESTRA, SENHA, fazer_pfx

import contas
import emissor_nfe
import nfe

N = {"n": nfe.NS}
DS = {"ds": "http://www.w3.org/2000/09/xmldsig#"}
QUANDO = dt.datetime(2026, 11, 20, 18, 0, tzinfo=dt.timezone.utc)
END_E = nfe.Endereco("RUA VAZ DE TOLEDO", "154", "ENGENHO NOVO", "3304557", "RIO DE JANEIRO", "RJ", "20780150")
END_SC = nfe.Endereco("Praça XV de Novembro", "S/N", "Centro", "4205407", "Florianópolis", "SC", "88010-400")
END_RJ = nfe.Endereco("Rua A", "1", "Centro", "3304557", "Rio de Janeiro", "RJ", "20000000")


def venda(**kw) -> nfe.Venda:
    base = {"emitente": nfe.Emitente(CNPJ, "EMPRESA & FILHOS LTDA", "16445924", "simples", END_E),
                "destinatario": nfe.Destinatario("07954480000179", "Cliente <SA>", END_SC),
                "itens": [nfe.Item("SKU1", "Bolsa", "42022210", D(2), D("150.00")),
                       nfe.Item("SKU2", "Carteira", "42023100", D(1), D("49.90"))],
                "pagamentos": [nfe.Pagamento("pix", D("369.90"))], "serie": 97, "numero": 1, "producao": False, "emitida_em": QUANDO,
                "frete": D("20.00"), "cnf": "12345678"}
    base.update(kw)
    return nfe.Venda(**base)


def arvore(xml: str):
    return lxml.fromstring(xml.encode())


# --------------------------------------------------------------------------------------------- montagem

def test_dv_da_chave_bate_com_a_chave_que_a_sefaz_autorizou_na_homologacao():
    real = "33261167520388000122550970000000011498366848"  # protocolo 333260001074257, 20/11/2026
    assert nfe.dv_mod11(real[:43]) == real[-1]


def test_frete_rateado_pelos_itens_fecha_com_o_total_e_o_vnf_soma_produtos_e_frete():
    _, xml = nfe.montar(venda(frete=D("10.00"), pagamentos=[nfe.Pagamento("pix", D("359.90"))]))
    r = arvore(xml)
    fretes = [D(x.text) for x in r.iterfind(".//n:det/n:prod/n:vFrete", N)]
    assert sum(fretes) == D("10.00") and len(fretes) == 2
    assert r.findtext(".//n:ICMSTot/n:vNF", namespaces=N) == "359.90"


def test_pagamento_que_nao_fecha_com_o_total_e_recusado_antes_da_sefaz():
    with pytest.raises(nfe.NfeInvalida, match="não fecham"):
        nfe.montar(venda(pagamentos=[nfe.Pagamento("pix", D(100))]))


def test_regime_normal_e_recusado_em_vez_de_sair_com_icms_errado():
    v = venda()
    v.emitente = nfe.Emitente(CNPJ, "X", "16445924", "normal", END_E)
    with pytest.raises(nfe.NfeInvalida, match="regime normal"):
        nfe.montar(v)


def test_cfop_de_dentro_da_uf_numa_venda_para_fora_e_recusado():
    v = venda(itens=[nfe.Item("SKU1", "Bolsa", "42022210", D(1), D("349.90"), cfop="5102")])
    with pytest.raises(nfe.NfeInvalida, match="CFOP"):
        nfe.montar(v)


def test_destino_e_cfop_padrao_seguem_a_uf_do_destinatario():
    fora = arvore(nfe.montar(venda())[1])
    dentro = arvore(nfe.montar(venda(destinatario=nfe.Destinatario("07954480000179", "C", END_RJ)))[1])
    assert fora.findtext(".//n:idDest", namespaces=N) == "2" and fora.findtext(".//n:CFOP", namespaces=N) == "6102"
    assert dentro.findtext(".//n:idDest", namespaces=N) == "1" and dentro.findtext(".//n:CFOP", namespaces=N) == "5102"


def test_homologacao_troca_o_nome_do_destinatario_pelo_texto_obrigatorio():
    r = arvore(nfe.montar(venda())[1])
    assert r.findtext(".//n:dest/n:xNome", namespaces=N) == nfe.XNOME_HOMOLOGACAO
    assert r.findtext(".//n:tpAmb", namespaces=N) == "2"


def test_venda_presencial_nao_leva_indintermed_e_a_nao_presencial_leva():
    assert "<indIntermed>" not in nfe.montar(venda(presencial=True))[1]
    assert "<indIntermed>0</indIntermed>" in nfe.montar(venda())[1]


def test_csosn_500_tem_grupo_proprio_e_os_outros_usam_icmssn102():
    v = venda(itens=[nfe.Item("A", "A", "42022210", D(1), D(100), csosn="400"),
                     nfe.Item("B", "B", "42022210", D(1), D("249.90"), csosn="500")])
    r = arvore(nfe.montar(v)[1])
    grupos = [lxml.QName(g[0]).localname for g in r.iterfind(".//n:ICMS", N)]
    assert grupos == ["ICMSSN102", "ICMSSN500"]


def test_texto_do_usuario_e_escapado_e_sem_espaco_entre_tags():
    xml = nfe.montar(venda(producao=True, informacoes="  pedido   123 & <cia>  "))[1]
    assert "Cliente &lt;SA&gt;" in xml and "pedido 123 &amp; &lt;cia&gt;" in xml and "> <" not in xml


def test_cnf_igual_ao_numero_e_trocado():
    chave, _ = nfe.montar(venda(numero=12345678, cnf="12345678"))
    assert chave[35:43] != "12345678"


def test_assinatura_sha1_confere_com_o_c14n_do_lxml_e_com_a_chave_publica():
    chave_priv, cert, _ = pkcs12.load_key_and_certificates(fazer_pfx(), SENHA.encode())
    _, xml = nfe.montar(venda())
    raiz = arvore(nfe.assinar(xml, chave_priv, cert))
    inf = raiz.find("n:infNFe", N)
    c14n = lxml.tostring(copy.deepcopy(inf), method="c14n")
    assert raiz.findtext(".//ds:DigestValue", namespaces=DS) == base64.b64encode(hashlib.sha1(c14n).digest()).decode()
    assert raiz.find(".//ds:SignatureMethod", DS).get("Algorithm").endswith("rsa-sha1")
    assert lxml.QName(raiz[1]).localname == "Signature"  # irmã de infNFe, dentro de NFe
    info = lxml.tostring(copy.deepcopy(raiz.find(".//ds:SignedInfo", DS)), method="c14n")
    cert.public_key().verify(base64.b64decode(raiz.findtext(".//ds:SignatureValue", namespaces=DS)), info,
                             padding.PKCS1v15(), hashes.SHA1())


def test_toda_uf_tem_autorizadora_com_os_quatro_servicos_nos_dois_ambientes():
    assert len(nfe.SERVIDORES["uf_autorizadora"]) == 27 == len(nfe.CUF)
    for uf in nfe.CUF:
        for producao in (True, False):
            for servico in nfe.SERVICOS:
                assert nfe.url(uf, producao, servico).startswith("https://")
    assert nfe.autorizadora("RJ") == "SVRS" and nfe.autorizadora("SP") == "SP" and nfe.autorizadora("MA") == "SVAN"


def test_situacao_atual_vem_do_retconssit_e_nao_do_protnfe_guardado():
    ret = lxml.fromstring(
        f'<retConsSitNFe xmlns="{nfe.NS}"><cStat>101</cStat><xMotivo>Cancelamento homologado</xMotivo>'
        f'<protNFe versao="4.00"><infProt><nProt>1</nProt><cStat>100</cStat><xMotivo>Autorizado</xMotivo></infProt></protNFe>'
        "</retConsSitNFe>")

    class S(nfe.Sefaz):
        def _chamar(self, servico, corpo):
            import xml.etree.ElementTree as ET
            return ET.fromstring(lxml.tostring(ret))
    q = S(uf="RJ", producao=False, pfx=b"", senha="").consultar("3" * 44)
    assert q.cstat == "101" and not q.autorizada


# --------------------------------------------------------------------------------------------- emissor

class SefazFalsa:
    roteiro: ClassVar[list] = []
    enviadas: ClassVar[list] = []
    consultas: ClassVar[list] = []
    autorizadas: ClassVar[dict] = {}

    def __init__(self, *, uf, producao, pfx, senha):
        self.uf, self.producao = uf, producao

    def autorizar(self, xml, lote):
        SefazFalsa.enviadas.append(xml)
        r = SefazFalsa.roteiro.pop(0)
        if r.autorizada:
            SefazFalsa.autorizadas[xml.split('Id="NFe')[1][:44]] = r
        return r

    def consultar(self, chave):
        SefazFalsa.consultas.append(chave)
        return SefazFalsa.autorizadas.get(chave) or nfe.Retorno(True, cstat="217", motivo="NF-e não consta")


def aut(prot="1") -> nfe.Retorno:
    return nfe.Retorno(True, cstat="100", motivo="Autorizado o uso da NF-e", protocolo=prot,
                       prot_xml=f'<protNFe versao="4.00"><infProt><nProt>{prot}</nProt><cStat>100</cStat></infProt></protNFe>')


CADASTRO = {"ie": "16445924", "logradouro": "Rua Vaz de Toledo", "numero": "154", "bairro": "Engenho Novo",
            "cep": "20780-150", "municipio": "Rio de Janeiro"}
PEDIDO = {"ref": "pedido-1", "destinatario": {"documento": "07.954.480/0001-79", "nome": "Cliente", "cep": "88010400",
                                              "logradouro": "Praça XV", "numero": "S/N", "bairro": "Centro",
                                              "municipio_ibge": "4205407", "municipio": "Florianópolis", "uf": "sc"},
          "itens": [{"codigo": "SKU1", "descricao": "Bolsa", "ncm": "4202.22.10", "quantidade": "2", "valor_unitario": "150,00"}],
          "frete": "10,00"}


@pytest.fixture()
def amb(tmp_path):
    SefazFalsa.roteiro, SefazFalsa.enviadas, SefazFalsa.consultas, SefazFalsa.autorizadas = [], [], [], {}
    c = contas.Contas(tmp_path / "n.db", b"s" * 32)
    u = c.entrar_google({"sub": "g", "email": "a@b.com", "verificado": True})
    e = c.cadastrar_empresa(u["id"], CNPJ, "Empresa", 3304557, "simples")
    c.guardar_certificado(u["id"], e["id"], fazer_pfx(), SENHA, MESTRA)
    em = emissor_nfe.EmissorNfe(c, producao=False, fabrica_sefaz=SefazFalsa)
    return c, em, u["id"], e["id"]


def _emitir(amb, pedido=PEDIDO):
    _, em, u, e = amb
    return em.emitir(u, e, pedido, limite_mes=None, mestra=MESTRA)


def _cadastrar(amb, **extra):
    _, em, u, e = amb
    return em.salvar_cadastro(u, e, CADASTRO | extra)


def test_sem_cadastro_fiscal_nao_emite_nem_gasta_numero(amb):
    with pytest.raises(contas.Erro) as e:
        _emitir(amb)
    assert e.value.codigo == "cadastro_nfe_incompleto" and SefazFalsa.enviadas == []


def test_uf_do_cadastro_vem_do_municipio_da_empresa_e_nao_do_que_se_digita(amb):
    assert _cadastrar(amb, uf="SP")["uf"] == "RJ"


def test_emitida_guarda_o_nfeproc_com_protocolo_e_o_pagamento_padrao_fecha_o_total(amb):
    _cadastrar(amb)
    SefazFalsa.roteiro = [aut("333")]
    n = _emitir(amb)
    assert n["status"] == "emitida" and n["numero"] == 1 and n["protocolo"] == "333" and n["valor"] == "310.00"
    _, em, u, e = amb
    chave, xml = em.xml(u, e, n["id"])
    assert xml.startswith("<?xml") and "<nfeProc" in xml and "<nProt>333</nProt>" in xml and chave in xml
    assert "<tPag>17</tPag><vPag>310.00</vPag>" in SefazFalsa.enviadas[0]


def test_mesmo_ref_nao_emite_segunda_nota(amb):
    _cadastrar(amb)
    SefazFalsa.roteiro = [aut()]
    a, b = _emitir(amb), _emitir(amb)
    assert a["id"] == b["id"] and len(SefazFalsa.enviadas) == 1


def test_sem_resposta_fica_desconhecida_e_a_volta_consulta_a_chave_antes_de_reenviar(amb):
    _cadastrar(amb)
    SefazFalsa.roteiro = [nfe.Retorno(False)]
    n = _emitir(amb)
    assert n["status"] == "desconhecida"
    SefazFalsa.autorizadas[n["chave"]] = aut("777")  # tinha saído
    n2 = _emitir(amb)
    assert n2["status"] == "emitida" and n2["protocolo"] == "777" and len(SefazFalsa.enviadas) == 1
    assert SefazFalsa.consultas == [n["chave"]]


def test_sem_resposta_e_nao_consta_reenvia_o_mesmo_xml_com_a_mesma_chave(amb):
    _cadastrar(amb)
    SefazFalsa.roteiro = [nfe.Retorno(False), aut()]
    n = _emitir(amb)
    n2 = _emitir(amb)
    assert n2["status"] == "emitida" and n2["chave"] == n["chave"] and SefazFalsa.enviadas[0] == SefazFalsa.enviadas[1]


def test_numero_usado_por_outro_sistema_pula_para_o_seguinte(amb):
    _cadastrar(amb)
    SefazFalsa.roteiro = [nfe.Retorno(True, cstat="539", motivo="Duplicidade de NF-e com diferença na chave"), aut()]
    n = _emitir(amb)
    assert n["status"] == "emitida" and n["numero"] == 2


def test_recusada_devolve_o_numero_e_a_cota_e_a_nota_seguinte_usa_o_mesmo_numero(amb):
    _cadastrar(amb)
    SefazFalsa.roteiro = [nfe.Retorno(True, cstat="225", motivo="Falha no Schema XML")]
    n = _emitir(amb)
    assert n["status"] == "recusada" and n["erros"][0]["codigo"] == "225"
    assert amb[0].empresa(amb[2], amb[3])["notas_no_mes"] == 0
    SefazFalsa.roteiro = [aut()]
    assert _emitir(amb, PEDIDO | {"ref": "pedido-2"})["numero"] == 1


def test_denegada_gasta_o_numero_e_nao_reenvia(amb):
    _cadastrar(amb)
    SefazFalsa.roteiro = [nfe.Retorno(True, cstat="301", motivo="Uso denegado")]
    n = _emitir(amb)
    assert n["status"] == "denegada" and _emitir(amb)["status"] == "denegada" and len(SefazFalsa.enviadas) == 1
    assert amb[1].cadastro(amb[2], amb[3])["proximo_teste"] == 2


def test_proximo_numero_continua_de_outro_sistema_e_nunca_desce(amb):
    assert _cadastrar(amb, proximo_teste="39")["proximo_teste"] == 39
    with pytest.raises(contas.Erro) as e:
        _cadastrar(amb, proximo_teste="10")
    assert e.value.codigo == "numero_para_tras"
    SefazFalsa.roteiro = [aut()]
    assert _emitir(amb)["numero"] == 39


def test_regime_normal_recusa_antes_de_falar_com_a_sefaz(amb):
    c, em, u, _ = amb
    e2 = c.cadastrar_empresa(u, "11222333000181", "Normal", 3304557, "normal")
    em.salvar_cadastro(u, e2["id"], CADASTRO)
    with pytest.raises(contas.Erro) as e:
        em.emitir(u, e2["id"], PEDIDO, limite_mes=None, mestra=MESTRA)
    assert e.value.codigo == "regime_sem_nfe" and SefazFalsa.enviadas == []


def test_servidor_sem_producao_recusa_nota_fiscal(amb):
    _cadastrar(amb)
    with pytest.raises(contas.Erro) as e:
        _emitir(amb, PEDIDO | {"ambiente": "producao"})
    assert e.value.status == 403 and SefazFalsa.enviadas == []


def test_item_invalido_vira_422_sem_gastar_numero(amb):
    _cadastrar(amb)
    with pytest.raises(contas.Erro) as e:
        _emitir(amb, PEDIDO | {"itens": [{"codigo": "A", "descricao": "A", "ncm": "4202", "valor_unitario": "dez"}]})
    assert e.value.status == 422
    assert amb[1].cadastro(amb[2], amb[3])["proximo_teste"] == 1


def test_imagem_leva_os_arquivos_que_a_nfe_le_do_disco():
    # O Dockerfile copiava só *.py: sem o mapa das SEFAZ e as raízes ICP-Brasil, o nfe.py nem importaria no pod.
    copia = next(l for l in (Path(__file__).resolve().parents[1] / "Dockerfile").read_text().splitlines()
                 if l.startswith("COPY *.py"))
    assert "sefaz_nfe.json" in copia and "icp-brasil-raizes.pem" in copia
