"""Emissor e cliente da Sefin. A Sefin é falsa (nenhuma nota real sai daqui); o TLS mútuo é de verdade, contra um
servidor HTTPS local que exige certificado de cliente."""
from __future__ import annotations

import base64
import gzip
import json
import os
import ssl
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import ClassVar

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.serialization import pkcs12

AQUI = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AQUI))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_contas import CNPJ, MESTRA, SENHA, fazer_pfx

import contas
import emissor
import sefin

PEDIDO = {"codigo_servico": "010101", "descricao": "Site institucional", "valor": "1500.00",
          "tomador": {"documento": "11222333000181", "nome": "Cliente Ltda"}}


def _gz(xml: str) -> str:
    return base64.b64encode(gzip.compress(xml.encode())).decode()


class SefinFalsa:
    """Roteiro: lista de respostas para cada chamada de enviar_dps; consultas respondem pelo `emitidas`."""
    roteiro: ClassVar[list] = []
    emitidas: ClassVar[dict] = {}
    enviadas: ClassVar[list] = []

    def __init__(self, *, producao, pfx, senha):
        assert pfx and senha  # o certificado chegou aberto do cofre
        self.producao = producao

    def enviar_dps(self, xml):
        SefinFalsa.enviadas.append(xml)
        return SefinFalsa.roteiro.pop(0)

    def consultar_dps(self, id_dps):
        chave = SefinFalsa.emitidas.get(id_dps)
        return sefin.Resposta(True, 200, {"chaveAcesso": chave}) if chave else sefin.Resposta(True, 404, {})

    def consultar_nfse(self, chave):
        return sefin.Resposta(True, 200, {"nfseXml": "<NFSe><nNFSe>9</nNFSe><vServ>1500.00</vServ><CNPJ>11222333000181</CNPJ></NFSe>"})


    eventos: ClassVar[list] = []
    evento_resposta: ClassVar[object] = None
    registrados: ClassVar[set] = set()

    def enviar_evento(self, chave, xml):
        SefinFalsa.eventos.append((self.producao, chave, xml))
        r = SefinFalsa.evento_resposta or sefin.Resposta(True, 201, {"eventoXml": "<evento/>"})
        if r.alcancou and r.status < 300:
            SefinFalsa.registrados.add(chave)
        return r

    def consultar_evento(self, chave, tipo, sequencia=1):
        return sefin.Resposta(True, 200 if chave in SefinFalsa.registrados else 404, {})


def ok(chave="1" * 50):
    return sefin.Resposta(True, 201, {"chaveAcesso": chave, "nfseXml": "<NFSe><nNFSe>1</nNFSe></NFSe>"})


@pytest.fixture()
def amb(tmp_path):
    SefinFalsa.roteiro, SefinFalsa.emitidas, SefinFalsa.enviadas = [], {}, []
    SefinFalsa.eventos, SefinFalsa.evento_resposta, SefinFalsa.registrados = [], None, set()
    c = contas.Contas(tmp_path / "n.db", b"s" * 32)
    u = c.entrar_google({"sub": "g", "email": "a@b.com", "verificado": True})
    e = c.cadastrar_empresa(u["id"], CNPJ, "Empresa", 4205407, "simples")
    c.guardar_certificado(u["id"], e["id"], fazer_pfx(), SENHA, MESTRA)
    em = emissor.Emissor(c, producao=False, fabrica_sefin=SefinFalsa)
    return c, em, u["id"], e["id"]


def _emitir(amb, pedido=PEDIDO, limite=10):
    _, em, u, e = amb
    return em.emitir(u, e, pedido, limite_mes=limite, mestra=MESTRA)


def test_emissao_aceita_vira_emitida_conta_na_cota_e_vai_assinada(amb):
    SefinFalsa.roteiro = [ok()]
    n = _emitir(amb)
    assert n["status"] == "emitida" and n["chave"] == "1" * 50 and n["numero_nfse"] == "1"
    assert "<Signature" in SefinFalsa.enviadas[0] and "<tpAmb>2</tpAmb>" in SefinFalsa.enviadas[0]
    assert amb[0].empresa(amb[2], amb[3])["notas_no_mes"] == 1


def test_mesmo_ref_nao_emite_segunda_nota(amb):
    SefinFalsa.roteiro = [ok()]
    a = _emitir(amb, PEDIDO | {"ref": "pedido-42"})
    b = _emitir(amb, PEDIDO | {"ref": "pedido-42"})
    assert a["id"] == b["id"] and len(SefinFalsa.enviadas) == 1


def test_recusa_do_governo_devolve_a_cota_e_guarda_o_erro(amb):
    SefinFalsa.roteiro = [sefin.Resposta(True, 400, {"erros": [{"Codigo": "E0312", "Descricao": "código inválido"}]})]
    n = _emitir(amb)
    assert n["status"] == "recusada" and n["erros"][0]["codigo"] == "E0312"
    assert amb[0].empresa(amb[2], amb[3])["notas_no_mes"] == 0


def test_municipio_inativo_no_teste_do_governo_vem_com_dica_de_que_nao_e_a_producao(amb):
    # Medido em 18/11: Ipu/CE inativo na produção restrita e ativo em produção; o texto cru (E0037) fazia parecer bug.
    SefinFalsa.roteiro = [sefin.Resposta(True, 400, {"erros": [{"Codigo": "E0037", "Descricao": "município inexistente"}]})]
    n = _emitir(amb)
    assert n["erros"][0]["codigo"] == "E0037" and "TESTE" in n["erros"][0]["dica"]
    assert emissor.com_dicas([{"codigo": "E0037", "mensagem": "x"}], True)[0]["dica"] != n["erros"][0]["dica"]
    desconhecido = emissor.com_dicas([{"codigo": "E9999", "mensagem": "x"}], False)[0]
    assert desconhecido["mensagem"] == "x" and desconhecido["dica"] == emissor.DICA_SEM_CODIGO


def test_resposta_perdida_fica_desconhecida_e_a_volta_consulta_antes_de_reenviar(amb):
    SefinFalsa.roteiro = [sefin.Resposta(False)]
    n = _emitir(amb, PEDIDO | {"ref": "r1"})
    assert n["status"] == "desconhecida"
    id_dps = amb[1].nota_por_id(n["id"])["id_dps"]
    SefinFalsa.emitidas[id_dps] = "7" * 50  # a nota tinha saído
    n2 = _emitir(amb, PEDIDO | {"ref": "r1"})
    assert n2["status"] == "emitida" and n2["chave"] == "7" * 50
    assert len(SefinFalsa.enviadas) == 1  # não reenviou


def test_e0014_de_nota_alheia_pula_o_numero_e_emite(amb):
    dup = sefin.Resposta(True, 400, {"erros": [{"Codigo": "E0014", "Descricao": "DPS já existe"}]})
    SefinFalsa.roteiro = [dup, ok("2" * 50)]
    n = _emitir(amb, PEDIDO | {"valor": "10.00"})  # a nota "achada" tem 1500.00: não é esta
    SefinFalsa.emitidas = {}
    assert n["status"] == "emitida" and n["numero"] == 2 and len(SefinFalsa.enviadas) == 2


def test_cota_esgotada_recusa_sem_chamar_o_governo(amb):
    SefinFalsa.roteiro = [ok()]
    _emitir(amb, limite=1)
    with pytest.raises(contas.Erro) as e:
        _emitir(amb, limite=1)
    assert e.value.status == 402 and len(SefinFalsa.enviadas) == 1


def test_empresa_sem_certificado_nao_emite(amb):
    c, em, u, e = amb
    c.apagar_certificado(u, e)
    with pytest.raises(contas.Erro) as err:
        em.emitir(u, e, PEDIDO, limite_mes=10, mestra=MESTRA)
    assert err.value.status == 409


# ----------------------------------------------------------- cliente Sefin

def test_decodificar_abre_campos_xml_gzip_base64():
    corpo = json.dumps({"chaveAcesso": "1", "nfseXmlGZipB64": _gz("<NFSe/>")}).encode()
    assert sefin.decodificar(corpo)["nfseXml"] == "<NFSe/>"
    assert "bruto" in sefin.decodificar(b"<html>403</html>")


def test_erro_sem_lista_vira_codigo_http_legivel():
    assert sefin.Resposta(True, 403, {"bruto": "<html>forbidden</html>"}).erros()[0]["codigo"] == "HTTP 403"


@pytest.mark.skipif(not hasattr(os, "memfd_create"), reason="memfd só no Linux")
def test_tls_mutuo_apresenta_o_certificado_do_cliente_sem_gravar_a_chave_em_disco(tmp_path):
    pfx = fazer_pfx()
    chave, cert, _ = pkcs12.load_key_and_certificates(pfx, SENHA.encode())
    # o servidor usa o mesmo par (autoassinado) como identidade e como CA do cliente
    pem_srv = tmp_path / "srv.pem"
    pem_srv.write_bytes(chave.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                            serialization.NoEncryption()) + cert.public_bytes(serialization.Encoding.PEM))
    vistos = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            vistos.append(self.connection.getpeercert(binary_form=True))
            corpo = json.dumps({"chaveAcesso": "9" * 50}).encode()
            self.send_response(201)
            self.send_header("Content-Length", str(len(corpo)))
            self.end_headers()
            self.wfile.write(corpo)

    srv = HTTPServer(("127.0.0.1", 0), H)
    ctx_srv = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx_srv.load_cert_chain(pem_srv)
    ctx_srv.verify_mode = ssl.CERT_REQUIRED
    ctx_srv.load_verify_locations(pem_srv)
    srv.socket = ctx_srv.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    antes = set(os.listdir(tempfile.gettempdir()))
    try:
        ctx = sefin.contexto_tls(pfx, SENHA)
        ctx.check_hostname = False
        ctx.load_verify_locations(pem_srv)
        r = sefin.Sefin(producao=False, pfx=pfx, senha=SENHA, host="127.0.0.1", porta=srv.server_port, contexto=ctx,
                        timeout=5).enviar_dps("<DPS/>")
    finally:
        srv.shutdown()
    assert r.alcancou and r.status == 201 and r.dados["chaveAcesso"] == "9" * 50
    assert vistos and vistos[0] == cert.public_bytes(serialization.Encoding.DER)
    novos = set(os.listdir(tempfile.gettempdir())) - antes
    assert not [f for f in novos if not f.startswith("pytest")], f"arquivo novo no tmp: {novos}"


def test_sem_resposta_do_governo_e_desconhecido_nao_erro():
    r = sefin.Sefin(producao=False, pfx=b"", senha="", host="127.0.0.1", porta=9, contexto=ssl.create_default_context(),
                    timeout=1).enviar_dps("<DPS/>")
    assert r.alcancou is False


MOTIVO = "Valor digitado errado na nota"


def test_cancelar_nota_emitida_manda_evento_assinado_e_marca_cancelada(amb):
    SefinFalsa.roteiro = [ok()]
    n = _emitir(amb)
    c = amb[1].cancelar(amb[2], amb[3], n["id"], motivo_codigo="1", motivo=MOTIVO, mestra=MESTRA)
    producao, chave, xml = SefinFalsa.eventos[0]
    assert c["status"] == "cancelada" and chave == "1" * 50 and "<Signature" in xml and "<e101101>" in xml
    assert producao is False and "<tpAmb>2</tpAmb>" in xml


def test_nota_de_teste_se_cancela_no_teste_mesmo_com_o_servidor_ja_em_producao(amb):
    # A virada para produção (18/11) não pode mandar o cancelamento de uma nota de teste para a produção.
    SefinFalsa.roteiro = [ok()]
    n = _emitir(amb)
    amb[1].producao = True
    amb[1].cancelar(amb[2], amb[3], n["id"], motivo_codigo="1", motivo=MOTIVO, mestra=MESTRA)
    assert SefinFalsa.eventos[0][0] is False and "<tpAmb>2</tpAmb>" in SefinFalsa.eventos[0][2]


def test_cancelar_de_novo_nao_manda_segundo_evento(amb):
    SefinFalsa.roteiro = [ok()]
    n = _emitir(amb)
    for _ in range(2):
        amb[1].cancelar(amb[2], amb[3], n["id"], motivo_codigo="1", motivo=MOTIVO, mestra=MESTRA)
    assert len(SefinFalsa.eventos) == 1


def test_resposta_perdida_no_cancelamento_consulta_o_evento_antes_de_dizer_que_falhou(amb):
    SefinFalsa.roteiro = [ok()]
    n = _emitir(amb)
    SefinFalsa.registrados.add("1" * 50)  # o governo registrou, mas a resposta não voltou
    SefinFalsa.evento_resposta = sefin.Resposta(False)
    c = amb[1].cancelar(amb[2], amb[3], n["id"], motivo_codigo="1", motivo=MOTIVO, mestra=MESTRA)
    assert c["status"] == "cancelada"


def test_cancelamento_recusado_mantem_a_nota_emitida_e_devolve_o_erro(amb):
    SefinFalsa.roteiro = [ok()]
    n = _emitir(amb)
    SefinFalsa.evento_resposta = sefin.Resposta(True, 400, {"erros": [{"Codigo": "E0840", "Descricao": "prazo esgotado"}]})
    with pytest.raises(contas.Erro) as e:
        amb[1].cancelar(amb[2], amb[3], n["id"], motivo_codigo="1", motivo=MOTIVO, mestra=MESTRA)
    assert e.value.status == 422 and "E0840" in str(e.value)
    assert amb[1].nota_por_id(n["id"])["status"] == "emitida"


def test_nao_cancela_nota_recusada_nem_com_motivo_curto(amb):
    SefinFalsa.roteiro = [sefin.Resposta(True, 400, {"erros": [{"Codigo": "E0312", "Descricao": "x"}]}), ok()]
    recusada = _emitir(amb, PEDIDO | {"ref": "r1"})
    emitida = _emitir(amb, PEDIDO | {"ref": "r2"})
    with pytest.raises(contas.Erro) as e1:
        amb[1].cancelar(amb[2], amb[3], recusada["id"], motivo_codigo="1", motivo=MOTIVO, mestra=MESTRA)
    with pytest.raises(contas.Erro) as e2:
        amb[1].cancelar(amb[2], amb[3], emitida["id"], motivo_codigo="1", motivo="curto", mestra=MESTRA)
    assert e1.value.status == 409 and e2.value.status == 422 and SefinFalsa.eventos == []


def test_sem_escolher_ambiente_a_nota_e_de_teste_mesmo_com_o_servidor_permitindo_valor_fiscal(amb):
    # Nota com valor fiscal só sai quando a pessoa escolhe; o padrão nunca gera imposto por engano.
    amb[1].producao = True
    SefinFalsa.roteiro = [ok()]
    n = _emitir(amb)
    assert n["producao"] is False and "<tpAmb>2</tpAmb>" in SefinFalsa.enviadas[0]


def test_ambiente_producao_escolhido_vai_com_tpamb_1_e_fica_marcado_na_nota(amb):
    amb[1].producao = True
    SefinFalsa.roteiro = [ok()]
    n = _emitir(amb, PEDIDO | {"ambiente": "producao"})
    assert n["producao"] is True and "<tpAmb>1</tpAmb>" in SefinFalsa.enviadas[0]


def test_servidor_sem_producao_recusa_nota_fiscal_antes_de_gastar_cota_ou_enviar(amb):
    with pytest.raises(contas.Erro) as e:
        _emitir(amb, PEDIDO | {"ambiente": "producao"})
    assert e.value.status == 403 and SefinFalsa.enviadas == []
    assert amb[0].empresa(amb[2], amb[3])["notas_no_mes"] == 0


def test_mesmo_ref_em_outro_ambiente_e_recusado_e_ambiente_invalido_e_422(amb):
    amb[1].producao = True
    SefinFalsa.roteiro = [sefin.Resposta(True, 400, {"erros": [{"Codigo": "E0312", "Descricao": "x"}]})]
    _emitir(amb, PEDIDO | {"ref": "p-1"})
    with pytest.raises(contas.Erro) as e1:
        _emitir(amb, PEDIDO | {"ref": "p-1", "ambiente": "producao"})
    with pytest.raises(contas.Erro) as e2:
        _emitir(amb, PEDIDO | {"ambiente": "homologacao"})
    assert e1.value.status == 409 and e2.value.status == 422


# --- Reforma Tributária e retenções ---------------------------------------------------------------------------------

CLASSE = {"cst": "000", "cclass_trib": "000001", "cind_op": "100301"}
# Trecho real da NFS-e nº 5 da produção restrita (20/11/2026, ISS retido + retenções federais + IBS/CBS).
NFSE_RTC = ("<NFSe><infNFSe><nNFSe>5</nNFSe><valores><vBC>100.00</vBC><pAliqAplic>2.00</pAliqAplic><vISSQN>2.00</vISSQN>"
            "<vTotalRet>13.50</vTotalRet><vLiq>86.50</vLiq></valores><IBSCBS><valores><vBC>94.35</vBC><uf><pIBSUF>0.10"
            "</pIBSUF></uf><mun><pIBSMun>0.00</pIBSMun></mun><fed><pCBS>0.90</pCBS></fed></valores><totCIBS><vTotNF>86.50"
            "</vTotNF><gIBS><vIBSTot>0.09</vIBSTot></gIBS><gCBS><vCBS>0.85</vCBS></gCBS></totCIBS></IBSCBS>"
            "<DPS><infDPS><valores><vServ>100.00</vServ></valores></infDPS></DPS></infNFSe></NFSe>")


def test_com_nbs_a_nota_leva_a_classificacao_do_motor_oficial_na_data_da_competencia(amb):
    pedidos = []
    amb[1].classificar = lambda nbs, data, escolha: pedidos.append((nbs, data)) or CLASSE
    SefinFalsa.roteiro = [ok()]
    n = _emitir(amb, PEDIDO | {"nbs": "115021000", "competencia": "2026-10-31"})
    assert pedidos == [("115021000", "2026-10-31")]
    assert "<cClassTrib>000001</cClassTrib>" in SefinFalsa.enviadas[0] and "<cIndOp>100301</cIndOp>" in SefinFalsa.enviadas[0]
    assert n["ibscbs"] == CLASSE | {"ind_final": "0"}


def test_classificacao_que_falha_recusa_antes_de_gastar_cota_e_de_falar_com_o_governo(amb):
    def ambigua(nbs, data, escolha):
        raise contas.Erro(422, "classificacao_ambigua", "escolha")
    amb[1].classificar = ambigua
    with pytest.raises(contas.Erro) as e:
        _emitir(amb, PEDIDO | {"nbs": "115021000"})
    assert e.value.codigo == "classificacao_ambigua" and SefinFalsa.enviadas == []
    assert amb[0].empresa(amb[2], amb[3])["notas_no_mes"] == 0


def test_ibscbs_false_emite_sem_o_grupo_e_sem_perguntar_ao_motor(amb):
    amb[1].classificar = lambda *a: pytest.fail("não devia classificar")
    SefinFalsa.roteiro = [ok()]
    _emitir(amb, PEDIDO | {"nbs": "115021000", "ibscbs": False})
    assert "IBSCBS" not in SefinFalsa.enviadas[0]


def test_sem_nbs_nao_ha_como_classificar_e_a_nota_sai_como_antes(amb):
    amb[1].classificar = lambda *a: pytest.fail("não devia classificar")
    SefinFalsa.roteiro = [ok()]
    _emitir(amb)
    assert "IBSCBS" not in SefinFalsa.enviadas[0]


def test_retencoes_em_reais_viram_tribfed_e_iss_retido_na_dps(amb):
    SefinFalsa.roteiro = [ok()]
    n = _emitir(amb, PEDIDO | {"retencoes": {"iss_retido": True, "aliquota_iss": "2", "irrf": "22,50", "csll": "15.00"}})
    xml = SefinFalsa.enviadas[0]
    assert "<tpRetISSQN>2</tpRetISSQN><pAliq>2.00</pAliq>" in xml and "<vRetIRRF>22.50</vRetIRRF><vRetCSLL>15.00</vRetCSLL>" in xml
    assert n["servico"]["retencoes"]["irrf"] == 2250  # o Repetir do painel refaz com as mesmas retenções


def test_retencao_que_nao_e_numero_e_422_sem_gastar_cota(amb):
    with pytest.raises(contas.Erro) as e:
        _emitir(amb, PEDIDO | {"retencoes": {"irrf": "dez reais"}})
    assert e.value.codigo == "retencao_invalida" and amb[0].empresa(amb[2], amb[3])["notas_no_mes"] == 0


def test_tributos_mostrados_sao_os_que_a_sefin_calculou_na_nfse_e_nao_os_da_dps():
    t = emissor.tributos_da_nota(NFSE_RTC)
    assert t == {"iss": "2.00", "aliquota_iss": "2.00", "retencoes": "13.50", "liquido": "86.50", "base_ibs_cbs": "94.35",
                 "cbs": "0.85", "ibs": "0.09", "aliquota_cbs": "0.90", "aliquota_ibs_uf": "0.10", "aliquota_ibs_mun": "0.00"}
    assert emissor.tributos_da_nota("") == {}


@pytest.mark.parametrize("escrito", ["1.000,00", "1000,00", "1000.00", "1000"])
def test_valor_escrito_com_milhar_e_virgula_e_aceito_como_mil_reais(amb, escrito):
    SefinFalsa.roteiro = [ok()]
    _emitir(amb, PEDIDO | {"valor": escrito})
    assert "<vServ>1000.00</vServ>" in SefinFalsa.enviadas[0]


def test_aliquota_do_simples_informada_vai_no_pTotTribSN_e_volta_para_o_repetir(amb):
    # Pedido de um usuário (21/11): o emissor oficial tem o campo; aqui toda nota de ME/EPP saía com 0.
    SefinFalsa.roteiro = [ok()]
    n = _emitir(amb, PEDIDO | {"aliquota_simples": "6,5"})
    assert "<pTotTribSN>6.50</pTotTribSN>" in SefinFalsa.enviadas[0]
    assert n["servico"]["aliquota_simples"] == "6.50"


def test_sem_aliquota_do_simples_a_nota_segue_saindo_com_zero_como_antes(amb):
    SefinFalsa.roteiro = [ok()]
    _emitir(amb)
    assert "<pTotTribSN>0</pTotTribSN>" in SefinFalsa.enviadas[0]


@pytest.mark.parametrize("ruim", ["seis", "-1", "0", "40"])
def test_aliquota_do_simples_absurda_e_422_sem_gastar_cota(amb, ruim):
    with pytest.raises(contas.Erro) as e:
        _emitir(amb, PEDIDO | {"aliquota_simples": ruim})
    assert e.value.codigo == "aliquota_simples_invalida" and amb[0].empresa(amb[2], amb[3])["notas_no_mes"] == 0


def test_mei_que_manda_aliquota_do_simples_e_recusado_antes_do_governo(amb):
    c, em, u, _ = amb
    mei = c.cadastrar_empresa(u, "11444777000161", "MEI", 4205407, "mei")
    with pytest.raises(contas.Erro) as e:
        em.emitir(u, mei["id"], PEDIDO | {"aliquota_simples": "6"}, limite_mes=10, mestra=MESTRA)
    assert e.value.codigo == "aliquota_simples_invalida" and SefinFalsa.enviadas == []


def test_conferir_devolve_todos_os_campos_que_faltam_de_uma_vez_sem_gastar_cota_nem_falar_com_o_governo(amb):
    # Pedido do X (21/11): o agente para antes de emitir e pergunta tudo ao usuário, não campo por campo a cada recusa.
    c, em, u, e = amb
    r = em.conferir(u, e, {"tomador": {"documento": "", "nome": "", "cep": "88010400"},
                           "retencoes": {"iss_retido": True}})
    campos = {f["campo"] for f in r["faltando"]}
    assert r["pronto"] is False
    assert {"codigo_servico", "descricao", "valor", "tomador.documento", "tomador.nome", "tomador.logradouro",
            "tomador.numero", "tomador.bairro", "tomador.municipio_ibge", "retencoes.aliquota_iss"} <= campos
    assert SefinFalsa.enviadas == [] and c.empresa(u, e)["notas_no_mes"] == 0


def test_conferir_pedido_completo_fica_pronto_mas_pergunta_aliquota_do_simples_e_ambiente(amb):
    _, em, u, e = amb
    r = em.conferir(u, e, PEDIDO)
    assert r["pronto"] is True and r["faltando"] == []
    assert {p["campo"] for p in r["perguntar"]} == {"aliquota_simples", "ambiente"}
    r = em.conferir(u, e, PEDIDO | {"aliquota_simples": "6", "ambiente": "teste"})
    assert r["pronto"] is True and r["perguntar"] == []


def test_conferir_pega_a_regra_da_dps_que_so_o_emitir_veria(amb):
    _, em, u, e = amb
    r = em.conferir(u, e, PEDIDO | {"codigo_servico": "12", "aliquota_simples": "6", "ambiente": "teste"})
    assert r["pronto"] is False and "cTribNac" in r["faltando"][0]["mensagem"]
    assert em.conferir(u, e, PEDIDO | {"aliquota_simples": "90"})["faltando"][0]["campo"] == "aliquota_simples_invalida"


def test_conferir_sem_certificado_avisa_antes_de_qualquer_outra_coisa(amb):
    c, em, u, _ = amb
    sem = c.cadastrar_empresa(u, "11444777000161", "Sem cert", 4205407, "simples")
    assert {"campo": "certificado"} .items() <= em.conferir(u, sem["id"], PEDIDO)["faltando"][0].items()


def test_banco_antigo_sem_as_colunas_novas_migra_e_continua_emitindo_com_im_e_regime_especial(tmp_path):
    import sqlite3
    caminho = tmp_path / "antigo.db"
    bd = sqlite3.connect(str(caminho))
    bd.executescript("""
    create table usuarios (id text primary key, google_sub text unique not null, email text not null,
      nome text not null default '', criado text not null);
    create table empresas (id text primary key, usuario_id text not null references usuarios(id), cnpj text unique not null,
      razao text not null, municipio_ibge integer not null, regime text not null default 'simples', criado text not null);
    insert into usuarios values ('usu_1', 'g1', 'ana@exemplo.com', 'Ana', '2026-11-01');
    """)
    bd.execute("insert into empresas values ('emp_1', 'usu_1', ?, 'Antiga', 4205407, 'simples', '2026-11-01')", (CNPJ,))
    bd.commit()
    bd.close()
    SefinFalsa.roteiro, SefinFalsa.emitidas, SefinFalsa.enviadas = [], {}, []
    SefinFalsa.eventos, SefinFalsa.evento_resposta, SefinFalsa.registrados = [], None, set()
    c = contas.Contas(caminho, b"s" * 32)
    c.guardar_certificado("usu_1", "emp_1", fazer_pfx(), SENHA, MESTRA)
    em = emissor.Emissor(c, producao=False, fabrica_sefin=SefinFalsa)
    SefinFalsa.roteiro = [ok()]
    assert em.emitir("usu_1", "emp_1", PEDIDO, limite_mes=10, mestra=MESTRA)["status"] == "emitida"
    assert "<regEspTrib>0</regEspTrib>" in SefinFalsa.enviadas[0] and "<IM>" not in SefinFalsa.enviadas[0]
    c.editar_empresa("usu_1", "emp_1", {"inscricao_municipal": "98765", "regime_especial": "6"})
    SefinFalsa.roteiro = [ok("2" * 50)]
    assert em.emitir("usu_1", "emp_1", PEDIDO | {"ref": "outra"}, limite_mes=10, mestra=MESTRA)["status"] == "emitida"
    assert f"<CNPJ>{CNPJ}</CNPJ><IM>98765</IM>" in SefinFalsa.enviadas[-1] and "<regEspTrib>6</regEspTrib>" in SefinFalsa.enviadas[-1]

PAGINA_403 = ('{"bruto": "<!DOCTYPE html PUBLIC \\"-//W3C//DTD XHTML 1.0 Strict//EN\\">\\r\\n<html><head><style>body{margin:0}</style>'
              '</head><body><h2>403 - Forbidden: Access is denied.</h2></body></html>"}')


def test_403_do_governo_vira_frase_e_dica_e_nao_a_pagina_html_inteira(amb):
    # O Rodrigo (X, 20/11) viu o HTML cru de um 403 do ambiente de teste e achou que o erro era dele.
    SefinFalsa.roteiro = [sefin.Resposta(True, 403, {"bruto": "<html><body>403 - Forbidden</body></html>"})]
    n = _emitir(amb)
    e = n["erros"][0]
    assert n["status"] == "recusada" and e["codigo"] == "HTTP 403"
    assert "<" not in e["mensagem"] and "TESTE" in e["mensagem"] and "cota" in e["dica"]


def test_nota_recusada_antes_da_explicacao_ganha_a_explicacao_na_leitura(amb):
    c, em, u, e = amb
    SefinFalsa.roteiro = [sefin.Resposta(True, 400, {"erros": [{"Codigo": "E0001", "Descricao": "x"}]})]
    n = _emitir(amb)
    c.bd.execute("update notas set erros = ? where id = ?",
                 (json.dumps([{"codigo": "HTTP 403", "mensagem": PAGINA_403}]), n["id"]))
    c.bd.commit()
    lida = em.notas(u, e)[0]["erros"][0]
    assert "DOCTYPE" not in lida["mensagem"] and lida["dica"]


def test_403_em_producao_aponta_o_certificado_e_no_teste_aponta_o_governo():
    prod = emissor.explicar({"codigo": "HTTP 403", "mensagem": PAGINA_403}, True)
    teste = emissor.explicar({"codigo": "HTTP 403", "mensagem": PAGINA_403}, False)
    assert "certificado" in prod["dica"] and "governo" in teste["dica"]


def test_erro_de_preenchimento_diz_que_nada_foi_enviado_nem_gastou_cota():
    e = emissor.explicar({"codigo": "entrada", "mensagem": "descreva o serviço"}, False)
    assert e["mensagem"] == "descreva o serviço" and "cota" in e["dica"]


def test_e0234_diz_para_preencher_o_endereco_do_tomador():
    assert "Endereço do tomador" in emissor.explicar({"codigo": "E0234", "mensagem": "x"}, True)["dica"]


def test_e0116_aponta_dados_fiscais_e_no_teste_sugere_emitir_com_valor_fiscal():
    # Rony (22/11): IM salva e mesmo assim E0116 no ambiente de teste; a tela só mostrava o texto do governo.
    teste = emissor.explicar({"codigo": "E0116", "mensagem": "A IM deve ser informada"}, False)["dica"]
    prod = emissor.explicar({"codigo": "E0116", "mensagem": "A IM deve ser informada"}, True)["dica"]
    assert "Dados fiscais" in teste and "TESTE" in teste and "CNC" in prod


def test_e0120_manda_apagar_a_inscricao_municipal():
    assert "Apague" in emissor.explicar({"codigo": "E0120", "mensagem": "x"}, True)["dica"]
