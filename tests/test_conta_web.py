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
from test_nfe import CADASTRO, PEDIDO, SefazFalsa, aut
from test_servidor import _subir

import conta_web
import contas
import emissor
import emissor_nfe
import planos
import servidor

ENV = {"NOTA_CHAVE_MESTRA": base64.b64encode(MESTRA).decode()}
# A mecânica de planos (assinatura no Pagar, teto de empresas) se testa numa tabela de exemplo: a real hoje é só o grátis.
LISTA = [
    {"id": "gratis", "nome": "Grátis", "preco_mes": 0, "notas_mes": None, "empresas": 1, "teste_dias": 0, "destaques": [], "link_pagar": ""},
    {"id": "pro", "nome": "Pro", "preco_mes": 29.9, "notas_mes": None, "empresas": None, "teste_dias": 0, "destaques": [],
     "link_pagar": "lnk_pro"},
]


@pytest.fixture()
def app(tmp_path, monkeypatch):
    SefinFalsa.roteiro, SefinFalsa.emitidas, SefinFalsa.enviadas = [], {}, []
    assinaturas = {}
    c = contas.Contas(tmp_path / "n.db", b"s" * 32)
    conta = conta_web.Conta(c, base="https://teste", env=ENV,
                            planos=planos.Planos(LISTA, consultar=lambda email: assinaturas.get(email, [])),
                            emissor=emissor.Emissor(c, producao=False, fabrica_sefin=SefinFalsa,
                                                           classificar=lambda nbs, data, escolha: {"cst": "000", "cclass_trib": "000001", "cind_op": "100301"}),
                            emissor_nfe=emissor_nfe.EmissorNfe(c, producao=False, fabrica_sefaz=SefazFalsa))
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
    esperado = ["", f"{planos.PAGAR}/p/lnk_pro"]
    assert st == 200 and [p["checkout"] for p in corpo["planos"]] == esperado
    assert corpo["sob_medida"]["whatsapp"].isdigit() and "@" in corpo["sob_medida"]["email"]


def test_plano_gratis_emite_sem_teto_de_notas_e_escritorio_cadastra_sem_teto_de_empresas(app):
    # "Quebra o mercado" (Thiago, 20/11): grátis ilimitado em notas para 1 CNPJ; só o número de CNPJs é cobrado.
    _, emp = _api(app, "POST", "/conta/empresas", {"cnpj": CNPJ, "razao": "A", "municipio_ibge": 4205407})
    for _ in range(50):
        app["c"].consumir_nota(emp["id"], None)
    assert app["c"].empresa(app["u"]["id"], emp["id"])["notas_no_mes"] == 50
    app["assinaturas"]["ana@exemplo.com"] = [{"link": "lnk_pro", "acesso": True, "status": "active"}]
    _api(app, "POST", "/conta/plano/atualizar")
    for i, cnpj in enumerate(("11222333000181", "45997418000153", "19131243000197")):
        st, _ = _api(app, "POST", "/conta/empresas", {"cnpj": cnpj, "razao": f"E{i}", "municipio_ibge": 4205407})
        assert st == 201


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


def test_tabela_publicada_e_gratis_sem_teto_de_notas_nem_de_empresas():
    # Ordem do Thiago (20/11): "faz ilimitado mesmo"; "tudo grátis por enquanto". Mudar isso é decisão dele, não acidente.
    gratis = planos.carregar()[0]
    assert (gratis["preco_mes"], gratis["notas_mes"], gratis["empresas"]) == (0, None, None)


def test_dois_planos_com_assinatura_ativa_a_conta_fica_com_o_mais_caro():
    lista = LISTA + [LISTA[1] | {"id": "barato", "preco_mes": 9.9, "empresas": 3, "link_pagar": "lnk_bar"}]
    links = [{"link": "lnk_pro", "acesso": True}, {"link": "lnk_bar", "acesso": True}]
    assert planos.Planos(lista, consultar=lambda e: links).do_email("x@y.com")["id"] == "pro"


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


def test_nfe_pela_porta_http_cadastro_por_put_emissao_e_xml_com_o_nome_da_chave(app):
    SefazFalsa.roteiro, SefazFalsa.enviadas, SefazFalsa.consultas, SefazFalsa.autorizadas = [aut("9")], [], [], {}
    _, emp = _api(app, "POST", "/conta/empresas", {"cnpj": CNPJ, "razao": "Loja", "municipio_ibge": 3304557})
    _api(app, "POST", f"/conta/empresas/{emp['id']}/certificado",
         {"pfx_base64": base64.b64encode(fazer_pfx()).decode(), "senha": SENHA})
    st, cad = _api(app, "PUT", f"/conta/empresas/{emp['id']}/nfe/cadastro", CADASTRO)
    assert st == 200 and cad["pronto"] and cad["uf"] == "RJ"
    st, nota = _api(app, "POST", f"/conta/empresas/{emp['id']}/nfe", PEDIDO)
    assert st == 201 and nota["status"] == "emitida"
    r = urllib.request.Request(f"{app['url']}/conta/empresas/{emp['id']}/nfe/{nota['id']}/xml",
                               headers={"Authorization": f"Bearer {app['chave']}"})
    with urllib.request.urlopen(r, timeout=10) as resp:
        corpo, cab = resp.read(), dict(resp.headers)
    assert b"<nfeProc" in corpo and cab["Content-Disposition"] == f'attachment; filename="{nota["chave"]}-nfe.xml"'
    _, lista = _api(app, "GET", f"/conta/empresas/{emp['id']}/nfe")
    assert [n["id"] for n in lista["notas"]] == [nota["id"]]


def test_suporte_publicado_cobra_100_por_mes_no_link_do_pagar_e_traz_o_whatsapp():
    # Ordem do Thiago (21/11): suporte de R$ 100 com link de pagamento, atendido no WhatsApp dele.
    sup = planos.por_id(planos.carregar())["suporte"]
    assert (sup["preco_mes"], sup["link_pagar"], sup["teste_dias"]) == (100, "9vsw2gjw", 0)
    assert sup["whatsapp"].isdigit() and sup["whatsapp"].startswith("55")
    assert sup["notas_mes"] is None and sup["empresas"] is None  # o suporte não tira nada do grátis


def test_botao_do_plano_pago_na_pagina_leva_ao_checkout_e_o_painel_mostra_o_whatsapp_de_quem_assinou():
    landing = (AQUI / "web" / "landing.js").read_text(encoding="utf-8")
    assert "p.preco_mes && p.checkout ? p.checkout" in landing
    assert "p.whatsapp" in (AQUI / "web" / "painel.js").read_text(encoding="utf-8")


def test_mcp_conferir_nfse_aceita_pedido_incompleto_e_lista_o_que_falta(app):
    _, emp = _api(app, "POST", "/conta/empresas", {"cnpj": CNPJ, "razao": "Empresa", "municipio_ibge": 4205407})
    r = _mcp(app, "conferir_nfse", {"empresa_id": emp["id"], "descricao": "Site"})["structuredContent"]
    assert r["pronto"] is False
    assert {"certificado", "codigo_servico", "valor"} <= {f["campo"] for f in r["faltando"]}
    assert SefinFalsa.enviadas == []


def test_patch_da_empresa_muda_im_e_regime_especial_e_recusa_valor_fora_do_leiaute_com_422(app):
    _, emp = _api(app, "POST", "/conta/empresas", {"cnpj": CNPJ, "razao": "Empresa", "municipio_ibge": 4205407})
    st, e = _api(app, "PATCH", f"/conta/empresas/{emp['id']}",
                 {"inscricao_municipal": "123.456", "regime_especial": "6", "cnpj": "11222333000181"})
    assert st == 200 and (e["inscricao_municipal"], e["regime_especial"], e["cnpj"]) == ("123456", "6", CNPJ)
    st, r = _api(app, "PATCH", f"/conta/empresas/{emp['id']}", {"inscricao_municipal": "1" * 16})
    assert st == 422 and r["erro"]["codigo"] == "inscricao_municipal_invalida"
    st, r = _api(app, "PATCH", f"/conta/empresas/{emp['id']}", {"regime_especial": "7"})
    assert st == 422 and r["erro"]["codigo"] == "regime_especial_invalido"
    _, eu = _api(app, "GET", "/conta/eu")
    assert eu["empresas"][0]["regime_especial"] == "6"


def test_patch_de_empresa_de_outra_conta_e_404_e_nao_muda_nada(app):
    c = app["c"]
    outro = c.entrar_google({"sub": "g2", "email": "beto@exemplo.com", "verificado": True})
    alheia = c.cadastrar_empresa(outro["id"], "11222333000181", "Do Beto", 4205407)
    st, r = _api(app, "PATCH", f"/conta/empresas/{alheia['id']}", {"inscricao_municipal": "999"})
    assert st == 404 and r["erro"]["codigo"] == "empresa_nao_encontrada"
    assert c.empresa(outro["id"], alheia["id"])["inscricao_municipal"] == ""


def test_mcp_editar_empresa_grava_im_e_regime_especial_pela_chave(app):
    _, emp = _api(app, "POST", "/conta/empresas", {"cnpj": CNPJ, "razao": "Empresa", "municipio_ibge": 4205407})
    st, r, _ = _req(app["url"] + "/mcp", "POST",
                    {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                     "params": {"name": "editar_empresa", "arguments": {"empresa_id": emp["id"], "inscricao_municipal": "4321",
                                                                        "regime_especial": "5"}}},
                    {"Authorization": f"Bearer {app['chave']}"})
    assert st == 200 and "isError" not in r["result"]
    e = app["c"].empresa(app["u"]["id"], emp["id"])
    assert (e["inscricao_municipal"], e["regime_especial"]) == ("4321", "5")
