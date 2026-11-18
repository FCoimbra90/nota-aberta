"""Testes da porta da nota-aberta contra um motor falso (o jar oficial não roda no CI).

A paridade com a Receita se prova com o motor de verdade: RTC_MOTOR_REAL=http://127.0.0.1:8081
roda também test_exemplo_oficial_da_receita_da_42_24_de_cbs (o item 3 de docs/exemplos.md do repo deles).
"""
from __future__ import annotations

import json
import os
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

AQUI = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AQUI))
import servidor


class MotorFalso(BaseHTTPRequestHandler):
    vistos: list = []  # noqa: RUF012 (registro do motor falso, zerado em cada teste)

    def log_message(self, *a):
        pass

    def _resp(self, status, obj):
        corpo = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(corpo)))
        self.end_headers()
        self.wfile.write(corpo)

    def do_GET(self):
        MotorFalso.vistos.append(("GET", self.path))
        if self.path.endswith("/versao"):
            return self._resp(200, {"versaoApp": "x"})
        self._resp(200, {"ok": True})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        MotorFalso.vistos.append(("POST", self.path, self.rfile.read(n)))
        self._resp(200, {"objetos": []})


def _subir(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture()
def porta(monkeypatch):
    motor = _subir(MotorFalso)
    monkeypatch.setattr(servidor, "BACKEND", f"http://127.0.0.1:{motor.server_port}")
    monkeypatch.setattr(servidor, "TETO", servidor.Teto(1000))
    MotorFalso.vistos = []
    srv = _subir(servidor.Porta)
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()
    motor.shutdown()


def _req(url, metodo="GET", corpo=None, cab=None):
    r = urllib.request.Request(url, data=corpo, method=metodo, headers=cab or {})
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def test_rota_interna_do_jar_como_actuator_nao_sai_para_a_internet(porta):
    st, _, _ = _req(porta + "/api/actuator/health")
    assert st == 404
    assert MotorFalso.vistos == []


def test_caminho_com_ponto_ponto_e_recusado_antes_de_chegar_ao_motor(porta):
    assert not servidor.permitida("GET", "/calculadora/dados-abertos/../../actuator/env")
    assert not servidor.permitida("GET", "/calculadora/dados-abertos//ufs")


def test_post_de_calculo_chega_ao_motor_com_o_mesmo_corpo(porta):
    corpo = json.dumps({"itens": [1]}).encode()
    st, dados, cab = _req(porta + "/api/calculadora/observabilidade/regime-geral", "POST", corpo,
                          {"Content-Type": "application/json"})
    assert st == 200 and json.loads(dados) == {"objetos": []}
    assert MotorFalso.vistos[-1] == ("POST", "/api/calculadora/observabilidade/regime-geral", corpo)
    assert cab["Access-Control-Allow-Origin"] == "*"


def test_corpo_acima_do_teto_volta_413_sem_ler_nem_repassar(porta):
    grande = b"x" * (servidor.CORPO_MAX + 1)
    st, _, _ = _req(porta + "/api/calculadora/regime-geral", "POST", grande, {"Content-Type": "application/json"})
    assert st == 413
    assert MotorFalso.vistos == []


def test_teto_por_ip_corta_no_pedido_seguinte_ao_limite(porta, monkeypatch):
    monkeypatch.setattr(servidor, "TETO", servidor.Teto(2))
    url = porta + "/api/calculadora/dados-abertos/ufs"
    cab = {"CF-Connecting-IP": "203.0.113.9"}
    assert [_req(url, cab=cab)[0] for _ in range(3)] == [200, 200, 429]
    assert _req(url, cab={"CF-Connecting-IP": "203.0.113.10"})[0] == 200


def test_teto_reabre_depois_de_um_minuto():
    agora = [0.0]
    t = servidor.Teto(1, relogio=lambda: agora[0])
    assert t.aceita("a") and not t.aceita("a")
    agora[0] = 60.0
    assert t.aceita("a")


def test_health_responde_503_quando_o_motor_esta_fora(monkeypatch):
    monkeypatch.setattr(servidor, "BACKEND", "http://127.0.0.1:9")
    srv = _subir(servidor.Porta)
    try:
        st, corpo, _ = _req(f"http://127.0.0.1:{srv.server_port}/health")
    finally:
        srv.shutdown()
    assert st == 503 and corpo == b"motor fora"


def test_pagina_tem_csp_e_estatico_nao_escapa_da_pasta_web(porta):
    st, corpo, cab = _req(porta + "/")
    assert st == 200 and "Nota fiscal de serviço de graça".encode() in corpo
    assert b"Calculadora de CBS e IBS" in _req(porta + "/calculadora.html")[1]
    assert "script-src 'self'" in cab["Content-Security-Policy"]
    assert _req(porta + "/../servidor.py")[0] == 404
    assert _req(porta + "/servidor.py")[0] == 404


def test_pagina_nao_calcula_tributo_no_navegador():
    """A conta é do motor oficial: o JS só monta a entrada. Multiplicar base por alíquota aqui seria
    a segunda cópia da regra fiscal, que diverge da Receita na primeira atualização."""
    js = (AQUI / "web" / "app.js").read_text(encoding="utf-8")
    assert "/observabilidade/regime-geral" in js
    assert "baseCalculo *" not in js and "* base" not in js


@pytest.mark.skipif(not os.environ.get("RTC_MOTOR_REAL"), reason="sem RTC_MOTOR_REAL (jar oficial rodando)")
def test_exemplo_oficial_da_receita_da_42_24_de_cbs(monkeypatch):
    monkeypatch.setattr(servidor, "BACKEND", os.environ["RTC_MOTOR_REAL"])
    srv = _subir(servidor.Porta)
    op = {"id": "t", "versao": "1.0.0", "dhFatoGerador": "2027-03-10T10:00:00-03:00", "municipio": 4314902, "uf": "RS",
          "itens": [{"numero": 1, "nbs": "122011200", "cst": "200", "cClassTrib": "200028", "baseCalculo": 1200.00,
                     "aliquotasNominais": {"cbs": 8.8, "ibsEstadual": 0.05, "ibsMunicipal": 0.05}}]}
    try:
        st, dados, _ = _req(f"http://127.0.0.1:{srv.server_port}/api/calculadora/observabilidade/regime-geral",
                            "POST", json.dumps(op).encode(), {"Content-Type": "application/json"})
    finally:
        srv.shutdown()
    g = json.loads(dados)["objetos"][0]["tribCalc"]["IBSCBS"]["gIBSCBS"]
    assert st == 200
    assert (g["gCBS"]["vCBS"], g["gCBS"]["gRed"]["pAliqEfet"], g["vIBS"]) == ("42.24", "3.52", "0.48")


def test_motor_compilado_e_o_commit_registrado_na_portaria_cotec_342():
    """Art. 2º, § 1º, I, b da Portaria Cotec nº 342/2026: o commit da versão publicada. Motor de outro commit
    sem paridade nova seria um número que a Receita não autorizou."""
    docker = (AQUI / "Dockerfile").read_text(encoding="utf-8")
    assert "ARG RTC_COMMIT=150cccbe414d4a5edea703e4f7a424af5aa222c7" in docker


def _rpc(url, corpo):
    st, dados, _ = _req(url + "/mcp", "POST", json.dumps(corpo).encode(), {"Content-Type": "application/json"})
    return st, (json.loads(dados) if dados else None)


def test_mcp_lista_calculo_so_leitura_e_emissao_marcada_como_escrita(porta):
    st, r = _rpc(porta, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    ferramentas = {f["name"]: f["annotations"]["readOnlyHint"] for f in r["result"]["tools"]}
    assert st == 200 and ferramentas == {"calcular_cbs_ibs": True, "tabelas_cbs_ibs": True, "aliquotas_referencia": True,
                                         "versao_motor": True, "listar_empresas": True, "emitir_nfse": False,
                                         "listar_notas": True}


def test_mcp_calcular_repassa_a_operacao_ao_motor_oficial_sem_mexer(porta):
    op = {"id": "x", "itens": [{"numero": 1, "cst": "000"}]}
    st, r = _rpc(porta, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                         "params": {"name": "calcular_cbs_ibs", "arguments": {"operacao": op}}})
    assert st == 200 and "isError" not in r["result"]
    metodo, caminho, corpo = MotorFalso.vistos[-1]
    assert (metodo, caminho, json.loads(corpo)) == ("POST", "/api/calculadora/observabilidade/regime-geral", op)


def test_mcp_data_malformada_vira_erro_de_ferramenta_sem_chamar_o_motor(porta):
    st, r = _rpc(porta, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                         "params": {"name": "tabelas_cbs_ibs", "arguments": {"data": "17/11/2026&x=1"}}})
    assert st == 200 and r["result"]["isError"] is True
    assert MotorFalso.vistos == []


def test_mcp_lote_nao_dribla_o_teto_por_ip(porta, monkeypatch):
    monkeypatch.setattr(servidor, "TETO", servidor.Teto(2))
    lote = [{"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": "versao_motor", "arguments": {}}}
            for i in range(4)]
    st, r = _rpc(porta, lote)
    assert st == 200 and [bool(x["result"].get("isError")) for x in r] == [False, False, True, True]
    assert len(MotorFalso.vistos) == 2


def test_mcp_notificacao_responde_202_sem_corpo(porta):
    st, r = _rpc(porta, {"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert st == 202 and r is None


@pytest.mark.skipif(not os.environ.get("RTC_MOTOR_REAL"), reason="sem RTC_MOTOR_REAL (jar oficial rodando)")
def test_mcp_com_motor_real_da_42_24_de_cbs_no_exemplo_da_receita(monkeypatch):
    monkeypatch.setattr(servidor, "BACKEND", os.environ["RTC_MOTOR_REAL"])
    monkeypatch.setattr(servidor, "TETO", servidor.Teto(1000))
    srv = _subir(servidor.Porta)
    op = {"id": "t", "versao": "1.0.0", "dhFatoGerador": "2027-03-10T10:00:00-03:00", "municipio": 4314902, "uf": "RS",
          "itens": [{"numero": 1, "nbs": "122011200", "cst": "200", "cClassTrib": "200028", "baseCalculo": 1200.00,
                     "aliquotasNominais": {"cbs": 8.8, "ibsEstadual": 0.05, "ibsMunicipal": 0.05}}]}
    try:
        _, r = _rpc(f"http://127.0.0.1:{srv.server_port}", {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                    "params": {"name": "calcular_cbs_ibs", "arguments": {"operacao": op}}})
    finally:
        srv.shutdown()
    g = r["result"]["structuredContent"]["objetos"][0]["tribCalc"]["IBSCBS"]["gIBSCBS"]
    assert g["gCBS"]["vCBS"] == "42.24"
