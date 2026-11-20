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
    assert emissor.com_dicas([{"codigo": "E9999", "mensagem": "x"}], False) == [{"codigo": "E9999", "mensagem": "x"}]


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
