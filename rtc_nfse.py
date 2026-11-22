"""Classificação da NFS-e na Reforma Tributária pelo NBS, perguntada ao motor oficial da Receita que roda no pod.

Nenhuma tabela é nossa: CST, cClassTrib e o código indicador da operação (cIndOp) vêm das rotas `calculadora/nfse/*`
do jar oficial (NfseController, release 1.6.1). Medido em 20/11/2026: NBS 115021000 -> CST 000, cClassTrib 000001
(tributação integral) e cIndOp 100301 (demais serviços, operações onerosas); a NFS-e nº 4 da produção restrita saiu
com essa classificação e a Sefin calculou CBS 0,90% e IBS 0,10% na própria nota.

Quando o motor devolve mais de uma opção, quem escolhe é a pessoa (ou o agente): nada de pegar a primeira.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

from contas import Erro

# chamar(metodo, caminho_api, corpo_bytes | None) -> (status, objeto JSON | None)
Chamador = Callable[[str, str, bytes | None], tuple[int, object]]


def chamar_motor(metodo: str, api: str, dados: bytes | None = None) -> tuple[int, object]:
    base = os.environ.get("RTC_BACKEND", "http://127.0.0.1:8081")
    req = urllib.request.Request(base + "/api" + api, data=dados, method=metodo,
                                 headers={"Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, None
    except (urllib.error.URLError, OSError, ValueError):
        return 502, None


def _lista(chamar: Chamador, api: str) -> list:
    status, dados = chamar("GET", api, None)
    if status >= 500 or (status == 200 and not isinstance(dados, list)):
        raise Erro(503, "motor_fora", "o motor oficial da Receita não respondeu; tente de novo em instantes "
                                      "ou emita sem o grupo IBS/CBS (ibscbs: false)")
    return dados if status == 200 else []


def opcoes(nbs: str, data: str, chamar: Chamador = chamar_motor) -> dict:
    """As classificações e os indicadores de operação que o motor aceita para o NBS na data."""
    q = urllib.parse.urlencode
    classes = _lista(chamar, "/calculadora/nfse/situacoes-classificacoes-tributarias?" + q({"nbs": nbs, "data": data}))
    indicadores = _lista(chamar, "/calculadora/nfse/indicador-operacao?"
                         + q({"dataOcorrenciaFatoGerador": data, "nbs": nbs}))
    return {
        "classificacoes": [{"cst": c.get("codigoSituacaoTributaria"), "cclass_trib": c.get("codigoClassificacaoTributaria"),
                            "descricao": c.get("descricaoClassificacaoTributaria")} for c in classes],
        "indicadores": [{"cind_op": i.get("cIndOp"), "descricao": i.get("tipoOperacao"),
                         "local_incidencia": i.get("localIncidencia")} for i in indicadores],
    }


def classificar(nbs: str, data: str, escolha: dict | None = None, chamar: Chamador = chamar_motor) -> dict:
    """{cst, cclass_trib, cind_op} para o NBS. `escolha` completa ou desempata; o que faltar e tiver uma opção só
    vem do motor. Mais de uma opção sem escolha = 422 com as opções, para quem emite decidir."""
    escolha = {k: str(v) for k, v in (escolha or {}).items() if v not in (None, "")}
    if {"cst", "cclass_trib", "cind_op"} <= escolha.keys():
        return escolha
    ops = opcoes(nbs, data, chamar)
    classes = [c for c in ops["classificacoes"]
               if escolha.get("cclass_trib") in (None, c["cclass_trib"]) and escolha.get("cst") in (None, c["cst"])]
    indicadores = [i for i in ops["indicadores"] if escolha.get("cind_op") in (None, i["cind_op"])]
    if not classes or not indicadores:
        raise Erro(422, "nbs_sem_classificacao", f"o motor oficial não tem classificação de IBS/CBS para o NBS {nbs} "
                                                 f"em {data}; confira o NBS ou emita com ibscbs: false")
    if len(classes) > 1 or len(indicadores) > 1:
        raise Erro(422, "classificacao_ambigua",
                   "este NBS tem mais de uma classificação de IBS/CBS; escolha em ibscbs: "
                   + json.dumps({"classificacoes": classes, "indicadores": indicadores}, ensure_ascii=False))
    return {"cst": classes[0]["cst"], "cclass_trib": classes[0]["cclass_trib"], "cind_op": indicadores[0]["cind_op"]}
