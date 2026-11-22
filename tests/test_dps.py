"""DPS e assinatura. A prova da assinatura é independente do código que assina: o C14N vem do lxml (a mesma
canonicalização que o verificador do governo faz) e a assinatura é conferida com a chave pública."""
from __future__ import annotations

import base64
import copy
import datetime as dt
import hashlib
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import pkcs12

lxml = pytest.importorskip("lxml.etree")

AQUI = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AQUI))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_contas import CNPJ, SENHA, fazer_pfx

import dps

EMISSAO = dt.datetime(2026, 11, 18, 15, 30, tzinfo=dt.timezone.utc)
P_SIMPLES = dps.Prestador(cnpj=CNPJ, municipio_ibge="4205407", regime="simples", aliquota_simples="6.00")
P_MEI = dps.Prestador(cnpj=CNPJ, municipio_ibge="4205407", regime="mei")
DSIG = {"ds": dps.DSIG}
NFSE = {"n": dps.NS}


def c14n(elemento) -> bytes:
    """C14N de um pedaço do documento. Copiado antes: o lxml 6.1.3 (libxml2 2.14.6) põe `xmlns=""` falso nos netos
    quando canonicaliza um subelemento no lugar (medido em 18/11/2026); separado, ele bate com o C14N 2.0."""
    return lxml.tostring(copy.deepcopy(elemento), method="c14n")


def _montar(producao=False, prestador=P_SIMPLES, **kw):
    base = {"producao": producao, "prestador": prestador, "serie": "1", "numero": 7, "emitida_em": EMISSAO,
            "competencia": "2026-11-18", "codigo_servico": "010101",
            "descricao": "Desenvolvimento de site & manutenção <mensal>", "valor_centavos": 150000, "nbs": "115021000",
            "tomador": dps.Tomador("11.222.333/0001-81", "Cliente Ltda", email="c@cliente.com")}
    base.update(kw)
    return dps.montar(**base)


def test_id_da_dps_segue_o_leiaute_municipio_tipo_cnpj_serie_numero():
    ident, _ = _montar()
    assert ident == f"DPS42054072{CNPJ}00001000000000000007" and len(ident) == 45


def test_xml_e_bem_formado_e_escapa_texto_do_usuario():
    _, xml = _montar()
    raiz = lxml.fromstring(xml.encode())
    assert raiz.findtext(".//n:xDescServ", namespaces=NFSE) == "Desenvolvimento de site & manutenção <mensal>"
    assert raiz.findtext(".//n:dhEmi", namespaces=NFSE) == "2026-11-18T12:30:00-03:00"


def test_producao_restrita_poe_cnbs_antes_da_descricao_e_exige_finnfse():
    _, restrita = _montar(producao=False)
    _, producao = _montar(producao=True)
    assert restrita.index("<cNBS>") < restrita.index("<xDescServ>") and "<finNFSe>0</finNFSe>" in restrita
    assert producao.index("<xDescServ>") < producao.index("<cNBS>") and "finNFSe" not in producao


def test_me_epp_informa_aliquota_do_simples_e_mei_nao_pode():
    _, simples = _montar(prestador=P_SIMPLES)
    _, mei = _montar(prestador=P_MEI)
    assert "<pTotTribSN>6.00</pTotTribSN>" in simples and "<regApTribSN>1</regApTribSN>" in simples
    assert "pTotTribSN" not in mei and "<indTotTrib>0</indTotTrib>" in mei and "regApTribSN" not in mei


@pytest.mark.parametrize("campo,valor", [("codigo_servico", "1010"), ("nbs", "12"), ("valor_centavos", 0),
                                         ("competencia", "2026-12-01"), ("descricao", "  ")])
def test_entrada_invalida_e_recusada_antes_de_ir_ao_governo(campo, valor):
    with pytest.raises(dps.DpsInvalida):
        _montar(**{campo: valor})


def test_assinatura_confere_com_o_c14n_do_lxml_e_com_a_chave_publica():
    chave, cert, _ = pkcs12.load_key_and_certificates(fazer_pfx(), SENHA.encode())
    ident, xml = _montar()
    assinado = dps.assinar(xml, chave, cert)
    raiz = lxml.fromstring(assinado.encode())
    inf = raiz.find("n:infDPS", namespaces=NFSE)
    digest = base64.b64encode(hashlib.sha256(c14n(inf)).digest()).decode()
    assert raiz.findtext(".//ds:DigestValue", namespaces=DSIG) == digest
    assert c14n(inf).decode() == lxml.canonicalize(lxml.tostring(inf).decode())  # dois C14N independentes concordam
    assert raiz.find(".//ds:Reference", namespaces=DSIG).get("URI") == f"#{ident}"
    signed_info = c14n(raiz.find(".//ds:SignedInfo", namespaces=DSIG))
    valor = base64.b64decode(raiz.findtext(".//ds:SignatureValue", namespaces=DSIG))
    cert.public_key().verify(valor, signed_info, padding.PKCS1v15(), hashes.SHA256())  # levanta se não bater


def test_assinatura_quebra_se_alguem_mexer_no_valor_depois():
    chave, cert, _ = pkcs12.load_key_and_certificates(fazer_pfx(), SENHA.encode())
    _, xml = _montar()
    adulterado = dps.assinar(xml, chave, cert).replace("<vServ>1500.00</vServ>", "<vServ>15.00</vServ>")
    raiz = lxml.fromstring(adulterado.encode())
    digest = base64.b64encode(hashlib.sha256(c14n(raiz.find("n:infDPS", namespaces=NFSE))).digest()).decode()
    assert raiz.findtext(".//ds:DigestValue", namespaces=DSIG) != digest


def test_cancelamento_tem_id_do_evento_e_motivo_com_15_caracteres_no_minimo():
    chave_nfse = "4" * 50
    xml = dps.cancelamento(producao=False, cnpj=CNPJ, chave=chave_nfse, motivo_codigo="1", motivo="erro", em=EMISSAO)
    raiz = lxml.fromstring(xml.encode())
    assert raiz.find("n:infPedReg", namespaces=NFSE).get("Id") == f"PRE{chave_nfse}101101"
    assert len(raiz.findtext(".//n:xMotivo", namespaces=NFSE)) == 15


def _toma(**end):
    return dps.Tomador("11.222.333/0001-81", "Prefeitura de Ipu", email="c@ipu.ce.gov.br", **end)


def _xml(tomador):
    return dps.montar(producao=True, prestador=dps.Prestador("11222333000181", "2305803", "simples"), serie="1", numero=1,
                      emitida_em=dt.datetime(2026, 11, 19, 12, tzinfo=dt.timezone.utc), competencia="2026-10-31",
                      codigo_servico="171001", descricao="Contrato", valor_centavos=100, tomador=tomador)[1]


def test_endereco_do_tomador_vai_na_ordem_do_xsd_entre_nome_e_email():
    # TCInfoPessoa: doc, IM, xNome, end, fone, email; TCEndereco: endNac(cMun, CEP), xLgr, nro, xCpl, xBairro.
    xml = _xml(_toma(cep="63250-000", municipio_ibge="2305803", logradouro="Praça da Matriz", numero="S/N", bairro="Centro"))
    assert ("<xNome>Prefeitura de Ipu</xNome><end><endNac><cMun>2305803</cMun><CEP>63250000</CEP></endNac>"
            "<xLgr>Praça da Matriz</xLgr><nro>S/N</nro><xBairro>Centro</xBairro></end><email>") in xml


def test_endereco_pela_metade_e_recusado_antes_de_ir_ao_governo():
    with pytest.raises(dps.DpsInvalida):
        _xml(_toma(cep="63250000", logradouro="Praça da Matriz"))
    with pytest.raises(dps.DpsInvalida):
        _xml(_toma(cep="6325", municipio_ibge="2305803", logradouro="Rua", numero="1", bairro="Centro"))
    assert "<end>" not in _xml(_toma())


# --- Reforma Tributária (IBSCBS) e retenções: formatos medidos na Sefin em 20/11/2026 -----------------------------

G = dps.IbsCbs(cst="000", cclass_trib="000001", cind_op="100301")


def test_ibscbs_na_producao_segue_o_esquema_antigo_que_a_sefin_de_producao_ainda_valida():
    _, xml = _montar(producao=True, ibscbs=G)
    grupo = lxml.fromstring(xml.encode()).find("n:infDPS/n:IBSCBS", NFSE)
    assert [lxml.QName(f).localname for f in grupo] == ["finNFSe", "indFinal", "cIndOp", "indDest", "valores"]
    assert grupo.findtext("n:valores/n:trib/n:gIBSCBS/n:CST", namespaces=NFSE) == "000"
    assert grupo.findtext("n:valores/n:trib/n:gIBSCBS/n:cClassTrib", namespaces=NFSE) == "000001"


def test_ibscbs_na_producao_restrita_vai_no_esquema_1_02_sem_finnfse_e_com_cst_direto_em_trib():
    _, xml = _montar(producao=False, ibscbs=G)
    inf = lxml.fromstring(xml.encode()).find("n:infDPS", NFSE)
    assert [lxml.QName(f).localname for f in inf][-2:] == ["valores", "IBSCBS"]
    grupo = inf.find("n:IBSCBS", NFSE)
    assert [lxml.QName(f).localname for f in grupo] == ["indFinal", "cIndOp", "indDest", "valores"]
    assert grupo.findtext("n:valores/n:trib/n:CST", namespaces=NFSE) == "000"


def test_sem_classificacao_a_dps_nao_leva_o_grupo_ibscbs():
    _, xml = _montar()
    assert "IBSCBS" not in xml


def test_classificacao_mal_formada_e_recusada_antes_de_ir_ao_governo():
    with pytest.raises(dps.DpsInvalida):
        _montar(ibscbs=dps.IbsCbs(cst="0", cclass_trib="000001", cind_op="100301"))


def test_iss_retido_e_retencoes_federais_vao_na_ordem_do_xsd():
    r = dps.Retencoes(iss_retido=True, aliquota_iss="2", irrf=2250, csll=1500, inss=16500, pis=975, cofins=4500,
                      pis_cofins_retidos=True)
    _, xml = _montar(producao=True, retencoes=r, ibscbs=G)
    trib = lxml.fromstring(xml.encode()).find("n:infDPS/n:valores/n:trib", NFSE)
    mun = trib.find("n:tribMun", NFSE)
    assert (mun.findtext("n:tpRetISSQN", namespaces=NFSE), mun.findtext("n:pAliq", namespaces=NFSE)) == ("2", "2.00")
    fed = trib.find("n:tribFed", NFSE)
    assert [lxml.QName(f).localname for f in fed] == ["piscofins", "vRetCP", "vRetIRRF", "vRetCSLL"]
    pc = fed.find("n:piscofins", NFSE)
    assert [lxml.QName(f).localname for f in pc] == ["CST", "vBCPisCofins", "vPis", "vCofins", "tpRetPisCofins"]
    assert (pc.findtext("n:vBCPisCofins", namespaces=NFSE), pc.findtext("n:tpRetPisCofins", namespaces=NFSE)) == ("1500.00", "3")
    assert (fed.findtext("n:vRetCP", namespaces=NFSE), fed.findtext("n:vRetIRRF", namespaces=NFSE),
            fed.findtext("n:vRetCSLL", namespaces=NFSE)) == ("165.00", "22.50", "15.00")
    assert [lxml.QName(f).localname for f in trib] == ["tribMun", "tribFed", "totTrib"]


def test_iss_retido_de_me_epp_sem_aliquota_e_recusado_como_a_sefin_faria_com_e0621():
    with pytest.raises(dps.DpsInvalida, match="alíquota"):
        _montar(retencoes=dps.Retencoes(iss_retido=True))


def test_retencoes_que_comem_o_valor_inteiro_sao_recusadas():
    with pytest.raises(dps.DpsInvalida, match="retenções"):
        _montar(retencoes=dps.Retencoes(irrf=150000))


def test_sem_retencao_o_trib_continua_igual_ao_que_a_producao_ja_aceitou():
    _, xml = _montar(producao=True)
    assert "<tribMun><tribISSQN>1</tribISSQN><tpRetISSQN>1</tpRetISSQN></tribMun>" in xml and "tribFed" not in xml


def test_assinatura_continua_conferindo_com_ibscbs_e_retencoes():
    _, xml = _montar(producao=True, ibscbs=G, retencoes=dps.Retencoes(irrf=1500))
    chave, cert, _ = pkcs12.load_key_and_certificates(fazer_pfx(), SENHA.encode())
    doc = lxml.fromstring(dps.assinar(xml, chave, cert).encode())
    inf = doc.find("n:infDPS", NFSE)
    digest = doc.findtext(".//ds:DigestValue", namespaces=DSIG)
    assert digest == base64.b64encode(hashlib.sha256(c14n(inf)).digest()).decode()


def test_dps_de_mei_com_aliquota_do_simples_e_invalida_antes_do_governo_recusar_com_E0710():
    with pytest.raises(dps.DpsInvalida):
        dps.montar(producao=True, prestador=dps.Prestador(CNPJ, "4205407", "mei", aliquota_simples="6.00"), serie="1",
                   numero=1, emitida_em=dt.datetime(2026, 11, 21, 12, tzinfo=dt.timezone.utc), competencia="2026-11-21",
                   codigo_servico="010101", descricao="x", valor_centavos=100)


def test_inscricao_municipal_informada_sai_no_prest_logo_depois_do_cnpj_e_antes_do_regtrib():
    p = dps.Prestador(cnpj=CNPJ, municipio_ibge="4205407", regime="simples", aliquota_simples="6.00",
                      inscricao_municipal="123456A")
    _, xml = _montar(prestador=p)
    assert f"<prest><CNPJ>{CNPJ}</CNPJ><IM>123456A</IM><regTrib>" in xml


def test_sem_inscricao_municipal_o_prest_nao_leva_im_vazia():
    _, xml = _montar()
    assert "<IM>" not in xml and f"<prest><CNPJ>{CNPJ}</CNPJ><regTrib>" in xml


def test_regime_especial_sociedade_de_profissionais_sai_6_no_regesptrib_e_nao_o_zero_fixo():
    p = dps.Prestador(cnpj=CNPJ, municipio_ibge="4205407", regime="normal", regime_especial="6")
    _, xml = _montar(prestador=p)
    assert "<regTrib><opSimpNac>1</opSimpNac><regEspTrib>6</regEspTrib></regTrib>" in xml


def test_regime_especial_fora_da_enumeracao_do_xsd_recusa_a_dps():
    for codigo in ("7", "8", "10", ""):
        with pytest.raises(dps.DpsInvalida):
            _montar(prestador=dps.Prestador(cnpj=CNPJ, municipio_ibge="4205407", regime="mei", regime_especial=codigo))
