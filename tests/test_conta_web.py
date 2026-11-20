"""Conta pela porta HTTP de verdade: chave de API, cookie com CSRF, plano pelo Genesis Pagar (falso), limite de
empresas e emissão ponta a ponta com a Sefin falsa."""
from __future__ import annotations

import base64
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

AQUI = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AQUI))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_contas import CNPJ, MESTRA, SENHA, fazer_pfx
from test_emissor import SefinFalsa, ok
from test_servidor import _subir

import conta_web
import contas
import emissor
import planos
import servidor

ENV = {"NOTA_CHAVE_MESTRA": base64.b64encode(MESTRA).decode()}
LISTA = planos.carregar()
LISTA[2] = LISTA[2] | {"link_pagar": "lnk_pro"}  # Profissional: 3 empresas


@pytest.fixture()
def app(tmp_path, monkeypatch):
    SefinFalsa.roteiro, SefinFalsa.emitidas, SefinFalsa.enviadas = [], {}, []
    assinaturas = {}
    c = contas.Contas(tmp_path / "n.db", b"s" * 32)
    conta = conta_web.Conta(c, base="https://teste", env=ENV,
                            planos=planos.Planos(LISTA, consultar=lambda email: assinaturas.get(email, [])),
                            emissor=emissor.Emissor(c, producao=False, fabrica_sefin=SefinFalsa))
    monkeypatch.setattr(servidor, "CONTA", conta)
    monkeypatch.setattr(servidor, "TETO", servidor.Teto(1000))
    srv = _subir(servidor.Porta)
    u = c.entrar_google({"sub": "g1", "email": "ana@exemplo.com", "verificado": True})
    yield {"url": f"http://127.0.0.1:{srv.server_port}", "c": c, "u": u, "chave": c.criar_chave(u["id"], "teste"),
           "assinaturas": assinaturas}
    srv.shutdown()


def _req(url, metodo="GET", corpo=None, cab=None):
    dados = json.dumps(corpo).encode() if corpo is not None else None
    r = urllib.request.Request(url, data=dados, method=metodo, headers={"Content-Type": "application/json", **(cab or {})})
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"null"), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null"), dict(e.headers)


def _api(app, metodo, caminho, corpo=None):
    return _req(app["url"] + caminho, metodo, corpo, {"Authorization": f"Bearer {app['chave']}"})[:2]


def test_conta_sem_sessao_nem_chave_e_401(app):
    assert _req(app["url"] + "/conta/eu")[0] == 401


def test_chave_de_api_invalida_e_401_mesmo_com_cookie(app):
    assert _req(app["url"] + "/conta/eu", cab={"Authorization": "Bearer na_falsa"})[0] == 401


def test_escrita_por_cookie_sem_cabecalho_x_nota_e_barrada_csrf(app):
    cookie = {"Cookie": f"na_sessao={app['c'].emitir_sessao(app['u']['id'])}"}
    st, corpo, _ = _req(app["url"] + "/conta/empresas", "POST", {"cnpj": CNPJ}, cookie)
    assert st == 403 and corpo["erro"]["codigo"] == "csrf"
    st, _, _ = _req(app["url"] + "/conta/eu", cab=cookie)
    assert st == 200


def test_ponta_a_ponta_empresa_certificado_e_nota_emitida_pela_api(app):
    st, emp = _api(app, "POST", "/conta/empresas", {"cnpj": CNPJ, "razao": "Empresa", "municipio_ibge": 4205407})
    assert st == 201
    st, cert = _api(app, "POST", f"/conta/empresas/{emp['id']}/certificado",
                    {"pfx_base64": base64.b64encode(fazer_pfx()).decode(), "senha": SENHA})
    assert st == 201 and cert["valido_ate"]
    SefinFalsa.roteiro = [ok()]
    st, nota = _api(app, "POST", f"/conta/empresas/{emp['id']}/notas",
                    {"codigo_servico": "010101", "descricao": "Site", "valor": "500,00", "ref": "pedido-1"})
    assert st == 201 and nota["status"] == "emitida" and nota["valor"] == 500.0
    st, lista = _api(app, "GET", f"/conta/empresas/{emp['id']}/notas")
    assert [n["ref"] for n in lista["notas"]] == ["pedido-1"]


def test_resposta_da_conta_nunca_traz_o_certificado_nem_a_senha(app):
    _, emp = _api(app, "POST", "/conta/empresas", {"cnpj": CNPJ, "razao": "Empresa", "municipio_ibge": 4205407})
    _api(app, "POST", f"/conta/empresas/{emp['id']}/certificado", {"pfx_base64": base64.b64encode(fazer_pfx()).decode(), "senha": SENHA})
    _, eu = _api(app, "GET", "/conta/eu")
    texto = json.dumps(eu)
    assert SENHA not in texto and "pfx" not in texto and "chave_embrulhada" not in texto


def test_plano_gratis_limita_a_uma_empresa_e_assinatura_ativa_libera_mais(app):
    _api(app, "POST", "/conta/empresas", {"cnpj": CNPJ, "razao": "A", "municipio_ibge": 4205407})
    st, corpo = _api(app, "POST", "/conta/empresas", {"cnpj": "11222333000181", "razao": "B", "municipio_ibge": 4205407})
    assert st == 402 and corpo["erro"]["codigo"] == "limite_empresas"
    app["assinaturas"]["ana@exemplo.com"] = [{"link": "lnk_pro", "acesso": True, "status": "trialing"}]
    _api(app, "POST", "/conta/plano/atualizar")
    st, _ = _api(app, "POST", "/conta/empresas", {"cnpj": "11222333000181", "razao": "B", "municipio_ibge": 4205407})
    assert st == 201


def test_assinatura_sem_acesso_cancelada_ou_atrasada_fica_no_gratis():
    p = planos.Planos(LISTA, consultar=lambda e: [{"link": "lnk_pro", "acesso": False, "status": "past_due"}])
    assert p.do_email("x@y.com")["id"] == "gratis"


def test_pagar_fora_do_ar_nao_da_plano_pago():
    p = planos.Planos(LISTA, consultar=lambda e: [])
    assert p.do_email("x@y.com")["id"] == "gratis"


def test_retorno_do_google_com_estado_trocado_e_recusado(app):
    st, corpo, _ = _req(app["url"] + "/entrar/google/retorno?state=outro&code=x", cab={"Cookie": "na_oauth=certo.verif"})
    assert st == 400 and corpo["erro"]["codigo"] == "estado_invalido"


def test_planos_publicos_sem_login_e_sem_link_nao_mostram_checkout(app):
    st, corpo, _ = _req(app["url"] + "/conta/planos")
    esperado = ["", f"{planos.PAGAR}/p/2bykhvut", f"{planos.PAGAR}/p/lnk_pro", f"{planos.PAGAR}/p/pxhgtuvj", f"{planos.PAGAR}/p/8ya9zfcp"]
    assert st == 200 and [p["checkout"] for p in corpo["planos"]] == esperado
    assert corpo["sob_medida"]["whatsapp"].isdigit() and "@" in corpo["sob_medida"]["email"]


def test_dois_planos_com_a_mesma_cota_de_notas_a_conta_fica_com_o_mais_caro():
    # Essencial e Profissional têm 300 notas; escolher pela cota daria ao assinante do Profissional só 1 empresa.
    lista = planos.carregar()
    links = [{"link": p["link_pagar"], "acesso": True} for p in lista if p["id"] in ("essencial", "profissional")]
    p = planos.Planos(lista, consultar=lambda e: links).do_email("x@y.com")
    assert p["id"] == "profissional" and p["empresas"] == 3


def test_plano_sem_carencia_nao_promete_teste_gratis():
    for p in planos.carregar():
        assert p["teste_dias"] == 0 or p["preco_mes"] > 0, p["id"]
        assert not (p["teste_dias"] == 0 and any("grátis" in d and "dias" in d for d in p["destaques"])), p["id"]


def test_todo_plano_pago_publicado_tem_link_do_pagar_e_o_gratis_nao_cobra():
    # Plano pago sem link some da tela e ninguém consegue assinar; grátis com link cobraria quem não deve.
    for p in planos.carregar():
        if p["preco_mes"] > 0:
            assert p["link_pagar"].isalnum() and len(p["link_pagar"]) == 8, p["id"]
        else:
            assert p["link_pagar"] == "", p["id"]


def test_sem_banco_configurado_a_conta_responde_503_e_a_calculadora_segue(monkeypatch):
    monkeypatch.setattr(servidor, "CONTA", None)
    srv = _subir(servidor.Porta)
    try:
        st, corpo, _ = _req(f"http://127.0.0.1:{srv.server_port}/conta/eu")
    finally:
        srv.shutdown()
    assert st == 503 and corpo["erro"]["codigo"] == "conta_desligada"
    assert servidor.montar_conta({}) is None


def test_certificado_de_outro_cnpj_volta_422_com_motivo(app):
    _, emp = _api(app, "POST", "/conta/empresas", {"cnpj": CNPJ, "razao": "Empresa", "municipio_ibge": 4205407})
    st, corpo = _api(app, "POST", f"/conta/empresas/{emp['id']}/certificado",
                     {"pfx_base64": base64.b64encode(fazer_pfx("99999999000199")).decode(), "senha": SENHA})
    assert st == 422 and "99999999000199" in corpo["erro"]["mensagem"]


def _mcp(app, nome, args, chave=True):
    corpo = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": nome, "arguments": args}}
    cab = {"Authorization": f"Bearer {app['chave']}"} if chave else {}
    return _req(app["url"] + "/mcp", "POST", corpo, cab)[1]["result"]


def test_mcp_emite_nfse_com_a_chave_da_conta_e_repete_ref_sem_duplicar(app):
    _, emp = _api(app, "POST", "/conta/empresas", {"cnpj": CNPJ, "razao": "Empresa", "municipio_ibge": 4205407})
    _api(app, "POST", f"/conta/empresas/{emp['id']}/certificado", {"pfx_base64": base64.b64encode(fazer_pfx()).decode(), "senha": SENHA})
    assert _mcp(app, "listar_empresas", {})["structuredContent"]["empresas"][0]["cnpj"] == CNPJ
    SefinFalsa.roteiro = [ok()]
    args = {"empresa_id": emp["id"], "codigo_servico": "010101", "descricao": "Site", "valor": "100.00", "ref": "ag-1"}
    a = _mcp(app, "emitir_nfse", args)["structuredContent"]
    b = _mcp(app, "emitir_nfse", args)["structuredContent"]
    assert a["status"] == "emitida" and a["id"] == b["id"] and len(SefinFalsa.enviadas) == 1


def test_mcp_de_emissao_sem_chave_volta_erro_de_ferramenta(app):
    r = _mcp(app, "emitir_nfse", {"empresa_id": "emp_x"}, chave=False)
    assert r["isError"] is True and "Bearer" in r["content"][0]["text"]


def test_pdf_da_nota_vem_do_danfse_da_receita_e_nota_alheia_e_404(app, monkeypatch):
    _, emp = _api(app, "POST", "/conta/empresas", {"cnpj": CNPJ, "razao": "Empresa", "municipio_ibge": 4205407})
    _api(app, "POST", f"/conta/empresas/{emp['id']}/certificado", {"pfx_base64": base64.b64encode(fazer_pfx()).decode(), "senha": SENHA})
    SefinFalsa.roteiro = [ok()]
    _, nota = _api(app, "POST", f"/conta/empresas/{emp['id']}/notas", {"codigo_servico": "010101", "descricao": "S", "valor": "1"})
    enviados = []
    monkeypatch.setattr(conta_web, "danfse", lambda xml, env: enviados.append(xml) or b"%PDF-1.7 teste")
    r = urllib.request.urlopen(urllib.request.Request(f"{app['url']}/conta/empresas/{emp['id']}/notas/{nota['id']}/pdf",
                                                      headers={"Authorization": f"Bearer {app['chave']}"}), timeout=10)
    assert r.headers["Content-Type"] == "application/pdf" and r.read().startswith(b"%PDF") and "<nNFSe>" in enviados[0]
    st, _ = _api(app, "GET", f"/conta/empresas/{emp['id']}/notas/nota_inexistente/pdf")
    assert st == 404


def test_sem_danfse_ligado_o_pdf_responde_503_e_nao_quebra():
    with pytest.raises(contas.Erro) as e:
        conta_web.danfse("<NFSe/>", {})
    assert e.value.status == 503


def test_cancelar_pela_api_e_pelo_mcp_e_nota_de_outra_empresa_e_404(app):
    _, emp = _api(app, "POST", "/conta/empresas", {"cnpj": CNPJ, "razao": "Empresa", "municipio_ibge": 4205407})
    _api(app, "POST", f"/conta/empresas/{emp['id']}/certificado", {"pfx_base64": base64.b64encode(fazer_pfx()).decode(), "senha": SENHA})
    SefinFalsa.roteiro = [ok("2" * 50), ok("3" * 50)]
    _, a = _api(app, "POST", f"/conta/empresas/{emp['id']}/notas", {"codigo_servico": "010101", "descricao": "S", "valor": "1", "ref": "a"})
    _, b = _api(app, "POST", f"/conta/empresas/{emp['id']}/notas", {"codigo_servico": "010101", "descricao": "S", "valor": "1", "ref": "b"})
    st, c = _api(app, "POST", f"/conta/empresas/{emp['id']}/notas/{a['id']}/cancelar", {"motivo": "Serviço não foi prestado"})
    assert st == 200 and c["status"] == "cancelada"
    r = _mcp(app, "cancelar_nfse", {"empresa_id": emp["id"], "nota_id": b["id"], "motivo_codigo": "2", "motivo": "Serviço não foi prestado"})
    assert r["structuredContent"]["status"] == "cancelada"
    st, _ = _api(app, "POST", f"/conta/empresas/{emp['id']}/notas/nota_de_outro/cancelar", {"motivo": "Serviço não foi prestado"})
    assert st == 404


def test_nota_listada_traz_o_servico_para_repetir_no_mes_seguinte(app):
    # Contrato de prefeitura: 80% das notas do contador são a mesma do mês anterior com outra competência.
    _, emp = _api(app, "POST", "/conta/empresas", {"cnpj": CNPJ, "razao": "Empresa", "municipio_ibge": 4205407})
    _api(app, "POST", f"/conta/empresas/{emp['id']}/certificado", {"pfx_base64": base64.b64encode(fazer_pfx()).decode(), "senha": SENHA})
    SefinFalsa.roteiro = [ok()]
    _api(app, "POST", f"/conta/empresas/{emp['id']}/notas", {"codigo_servico": "171001", "nbs": "115021000", "descricao": "Contrato 12/2026",
         "valor": "980.00", "competencia": "2026-10-31", "tomador": {"documento": "11222333000181", "nome": "Prefeitura"}})
    _, lista = _api(app, "GET", f"/conta/empresas/{emp['id']}/notas")
    s = lista["notas"][0]["servico"]
    assert (s["codigo_servico"], s["nbs"], s["competencia"], s["tomador"]["nome"]) == ("171001", "115021000", "2026-10-31", "Prefeitura")
    assert "<dCompet>2026-10-31</dCompet>" in SefinFalsa.enviadas[0] and "<cNBS>115021000</cNBS>" in SefinFalsa.enviadas[0]
