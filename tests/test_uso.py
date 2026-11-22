"""Painel de uso (/operacao): contadores por minuto sem dado pessoal, só para e-mail da lista, pela porta HTTP de verdade."""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

AQUI = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AQUI))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_conta_web import _req, app  # noqa: F401 (fixture reaproveitada)

import servidor
import uso

UA_IPHONE_X = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
               "Mobile/15E148 Twitter for iPhone/10.70")


class Relogio:
    def __init__(self, t=1_763_700_000.0):
        self.t = t

    def __call__(self):
        return self.t


def _uso(tmp_path, **kw):
    return uso.Uso(tmp_path / "uso.db", b"k" * 32, relogio=kw.pop("relogio", Relogio()), **kw)


def test_navegador_dentro_do_app_do_x_conta_como_pessoa_e_o_robo_da_previa_como_robo():
    assert uso.quem(UA_IPHONE_X) == "pessoa"
    assert uso.quem("Twitterbot/1.0") == "robo"
    assert uso.quem("facebookexternalhit/1.1") == "robo"
    assert uso.quem("ClaudeBot/1.0") == "agente"
    assert uso.quem("python-urllib/3.12") == "agente"
    assert uso.quem("") == "agente"


def test_sonda_do_kubernetes_e_o_proprio_painel_nao_inflam_o_contador():
    assert uso.canal("GET", "/health") is None
    assert uso.canal("GET", "/operacao/stats.json") is None
    assert uso.canal("POST", "/conta/empresas/emp_1/notas") == "emissao"
    assert uso.canal("POST", "/conta/empresas/emp_1/nfe") == "emissao"
    assert uso.canal("GET", "/conta/empresas/emp_1/notas") == "conta"
    assert uso.canal("POST", "/api/calculadora/regime-geral") == "calculadora"


def test_link_do_x_vem_como_t_co_e_o_proprio_site_como_direto():
    assert uso.origem("https://t.co/abc", "reforma.genesisinnovation.io") == "x.com (t.co)"
    assert uso.origem("https://reforma.genesisinnovation.io/painel.html", "reforma.genesisinnovation.io") == "direto"
    assert uso.origem("", "x") == "direto"
    assert uso.origem("https://l.instagram.com/?u=1", "x") == "instagram.com"


def test_banco_do_painel_nao_guarda_ip_nem_e_mail_so_contagem_e_hash_do_dia(tmp_path):
    u = _uso(tmp_path)
    cab = {"user-agent": UA_IPHONE_X, "referer": "https://t.co/x", "cf-ipcountry": "BR"}
    for _ in range(3):
        u.registrar("GET", "/", 200, 12.0, cab, "200.100.50.25")
    u.registrar("GET", "/", 200, 12.0, cab, "200.100.50.26")
    u.despejar()
    despejo = "\n".join(sqlite3.connect(tmp_path / "uso.db").iterdump())
    assert "200.100.50" not in despejo
    est = u.estatisticas()
    assert est["visitantes"]["hoje"] == 2
    assert est["hoje"]["total"] == 4 and est["hoje"]["pessoa"] == 4 and est["hoje"]["pagina"] == 4
    assert {"chave": "x.com (t.co)", "n": 4} in est["top"]["origem"]
    assert {"chave": "BR", "n": 4} in est["top"]["pais"]


def test_visitante_de_um_dia_nao_se_liga_ao_do_dia_seguinte(tmp_path):
    r = Relogio()
    u = _uso(tmp_path, relogio=r)
    u.registrar("GET", "/", 200, 1, {"user-agent": UA_IPHONE_X}, "1.2.3.4")
    r.t += 86400
    u.registrar("GET", "/", 200, 1, {"user-agent": UA_IPHONE_X}, "1.2.3.4")
    u.despejar()
    hashes = [h for (h,) in sqlite3.connect(tmp_path / "uso.db").execute("select h from uso_vis")]
    assert len(hashes) == 2 and hashes[0] != hashes[1]


def test_ranking_esconde_a_cauda_que_aparece_uma_vez_so(tmp_path):
    u = _uso(tmp_path)
    cab = {"user-agent": UA_IPHONE_X}
    u.registrar("GET", "/", 200, 1, {**cab, "referer": "https://raro.example/perfil-de-alguem"}, "1.1.1.1")
    u.registrar("POST", "/mcp", 200, 1, {"user-agent": "ClaudeBot"}, "", ["calcular_tributos"])
    u.registrar("POST", "/mcp", 200, 1, {"user-agent": "ClaudeBot"}, "", ["calcular_tributos"])
    top = u.estatisticas()["top"]
    assert top["origem"] == []
    assert top["ferramenta"] == [{"chave": "calcular_tributos", "n": 2}]


def test_minuto_por_minuto_soma_entre_despejos_sem_perder_contagem(tmp_path):
    u = _uso(tmp_path)
    u.registrar("GET", "/", 200, 10, {}, "")
    u.despejar()
    u.registrar("GET", "/", 503, 30, {}, "")
    est = u.estatisticas()
    assert est["hoje"]["total"] == 2 and est["hoje"]["s5"] == 1 and est["hoje"]["lat_media_ms"] == 20
    assert est["timeline"]["m60"][-1]["total"] == 2


def test_contagem_de_negocio_le_o_banco_das_contas_sem_tabelas_do_emissor(app):  # noqa: F811
    ler = uso.negocio_do_banco(app["c"].bd.execute("pragma database_list").fetchone()[2])
    g = ler()
    assert g["contas"] == 1 and g["empresas"] == 0
    assert g["nfse"]["hoje"] == 0 and g["nfe"]["producao"] == {}


@pytest.fixture()
def painel(app, tmp_path, monkeypatch):  # noqa: F811
    u = _uso(tmp_path, relogio=__import__("time").time, negocio=lambda: {"contas": 1})
    monkeypatch.setattr(servidor, "USO", u)
    monkeypatch.setenv("NOTA_PAINEL_EMAILS", "outra@exemplo.com, Ana@Exemplo.com")
    sessao = app["c"].emitir_sessao(app["u"]["id"])
    return {**app, "uso": u, "cookie": {"Cookie": f"na_sessao={sessao}"}}


def _bruto(url, cab=None):
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=cab or {}), timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_painel_sem_login_mostra_so_o_botao_de_entrar_e_os_numeros_dao_403(painel):
    st, corpo = _bruto(painel["url"] + "/operacao")
    assert st == 403 and b"/entrar/google?volta=/operacao" in corpo and b"stats.json" not in corpo
    assert _bruto(painel["url"] + "/operacao/stats.json")[0] == 403


def test_pagina_do_painel_nao_abre_pelo_caminho_estatico_sem_conferir_login(painel):
    assert _bruto(painel["url"] + "/operacao.html")[0] == 404
    assert _bruto(painel["url"] + "/operacao-entrar.html")[0] == 404
    assert _bruto(painel["url"] + "/operacao.js")[0] == 200


def test_conta_logada_fora_da_lista_nao_ve_os_numeros(painel, monkeypatch):
    monkeypatch.setenv("NOTA_PAINEL_EMAILS", "")
    assert _bruto(painel["url"] + "/operacao/stats.json", painel["cookie"])[0] == 403


def test_email_da_lista_ve_o_painel_e_os_pedidos_aparecem_contados(painel):
    _bruto(painel["url"] + "/", {"User-Agent": UA_IPHONE_X})
    _bruto(painel["url"] + "/health")
    st, corpo = _bruto(painel["url"] + "/operacao", painel["cookie"])
    assert st == 200 and b"operacao.js?v=" in corpo
    st, corpo = _bruto(painel["url"] + "/operacao/stats.json", painel["cookie"])
    est = json.loads(corpo)
    assert st == 200 and est["negocio"] == {"contas": 1}
    assert est["hoje"]["pagina"] == 1 and est["hoje"]["pessoa"] == 1  # /health e o próprio painel ficam fora


def test_mcp_conta_o_nome_da_ferramenta_e_nada_do_pedido(painel):
    corpo = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "calcular_tributos", "arguments": {"cnpj": "11222333000181"}}}
    for _ in range(2):
        _req(painel["url"] + "/mcp", "POST", corpo)
    est = painel["uso"].estatisticas()
    assert est["top"]["ferramenta"] == [{"chave": "calcular_tributos", "n": 2}]
    assert "11222333000181" not in json.dumps(est)


def test_login_com_volta_para_o_painel_aceita_so_destino_da_lista():
    import conta_web
    assert conta_web.VOLTAS == ("/operacao",)
    assert uso.autorizado("ANA@exemplo.com", " ana@exemplo.com ,x@y")
    assert not uso.autorizado("", "")
    assert not uso.autorizado("ana@exemplo.com", "")


def test_latencia_nao_conta_o_tempo_parado_da_conexao_keep_alive(painel):
    import http.client
    import time
    host, porta_ = painel["url"].removeprefix("http://").split(":")
    c = http.client.HTTPConnection(host, int(porta_), timeout=5)
    c.request("GET", "/", headers={"User-Agent": UA_IPHONE_X})
    c.getresponse().read()
    time.sleep(1.5)  # a conexão fica aberta e parada, como num navegador
    c.request("GET", "/calculadora.html", headers={"User-Agent": UA_IPHONE_X})
    c.getresponse().read()
    c.close()
    time.sleep(0.2)
    est = painel["uso"].estatisticas()
    assert est["hoje"]["pagina"] == 2
    assert est["hoje"]["lat_media_ms"] < 500


def test_funil_e_lista_mostram_onde_a_empresa_parou_sem_email_nem_cnpj(app):  # noqa: F811
    import json as _json

    import contas
    c = app["c"]
    e1 = c.cadastrar_empresa(app["u"]["id"], "11222333000181", "Clinica Boa", 3550308, "simples")
    c.bd.execute("insert into notas (id, empresa_id, ref, status, pedido, numero, producao, mes, criada, atualizada, erros) "
                 "values ('n1', ?, 'r1', 'recusada', '{}', 1, 0, '2026-11', '2026-11-21T03:00', '2026-11-21T03:00', ?)",
                 (e1["id"], _json.dumps([{"codigo": "HTTP 403", "mensagem": "<html><body>403 - Forbidden: Access is denied.</body></html>"}])))
    c.bd.commit()
    g = uso.negocio_do_banco(c.bd.execute("pragma database_list").fetchone()[2])()
    f = g["funil"]
    assert f["contas"] == 1 and f["com_empresa"] == 1 and f["emitiu"] == 0 and f["emitiu_producao"] == 0
    assert f["contas"] >= f["com_empresa"] >= f["com_certificado"] >= f["emitiu"] >= f["emitiu_producao"]
    linha = g["lista"][0]
    assert linha["razao"] == "Clinica Boa" and linha["etapa"] == "travou no envio" and linha["recusadas"] == 1
    assert "<" not in linha["ultimo_erro"]["mensagem"] and "TESTE" in linha["ultimo_erro"]["mensagem"]
    assert linha["ultimo_erro"]["dica"]  # o dono vê a mesma explicação que a pessoa
    tudo = _json.dumps(g)
    assert "11222333000181" not in tudo and app["u"]["email"] not in tudo
    assert contas  # import usado: a fixture cria a conta pelo módulo real
