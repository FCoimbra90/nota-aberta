"""MCP da nota-aberta (JSON-RPC 2.0, Streamable HTTP só com POST e resposta JSON, sem sessão).

Mesmo formato do agenda-clinica (apps/sistemas/agenda-clinica/app.py). Cada ferramenta é uma chamada ao motor
oficial pela mesma função que a API pública usa; nenhuma conta de tributo acontece aqui.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable

VERSOES = ("2025-06-18", "2025-03-26", "2024-11-05")
MAX_LOTE = 10
INSTRUCOES = (
    "Calcula CBS, IBS (estadual e municipal) e Imposto Seletivo da Reforma Tributária (LC 214/2025) com o motor "
    "oficial que a Receita Federal publicou em MIT (Portaria Cotec 342/2026), sem alteração. Fluxo comum: "
    "tabelas_cbs_ibs(data) para escolher CST e cClassTrib, aliquotas_referencia(data, uf, municipio), depois "
    "calcular_cbs_ibs(operacao). Até 31/12/2026 não informe aliquotasNominais (o motor recusa com REG-036); a partir "
    "de 2027 elas são obrigatórias. Resultado informativo, sem efeito vinculante."
)

DATA = {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$", "description": "AAAA-MM-DD"}
FERRAMENTAS = [
    {"name": "calcular_cbs_ibs",
     "description": "Calcula CBS, IBS e IS de uma operação do regime regular, item a item, com memória de cálculo. "
                    "A entrada é a mesma do POST /api/calculadora/observabilidade/regime-geral (id, versao, "
                    "dhFatoGerador, municipio (código IBGE), uf, itens[] com numero, ncm ou nbs, cst, cClassTrib, "
                    "baseCalculo e, a partir de 2027, aliquotasNominais).",
     "inputSchema": {"type": "object", "required": ["operacao"],
                     "properties": {"operacao": {"type": "object", "description": "a operação, como na API"}}},
     "annotations": {"readOnlyHint": True, "openWorldHint": False}},
    {"name": "tabelas_cbs_ibs",
     "description": "Situações tributárias (CST) e classificações (cClassTrib) vigentes na data, com reduções e se o "
                    "item usa NCM ou NBS.",
     "inputSchema": {"type": "object", "required": ["data"], "properties": {"data": DATA}},
     "annotations": {"readOnlyHint": True, "openWorldHint": False}},
    {"name": "aliquotas_referencia",
     "description": "Alíquotas de referência da CBS (União), do IBS estadual e do IBS municipal na data. Hoje o "
                    "motor só tem as de 2026.",
     "inputSchema": {"type": "object", "required": ["data"],
                     "properties": {"data": DATA,
                                    "codigo_uf": {"type": "integer", "description": "código IBGE da UF, ex.: 42"},
                                    "codigo_municipio": {"type": "integer", "description": "código IBGE, 7 dígitos"}}},
     "annotations": {"readOnlyHint": True, "openWorldHint": False}},
    {"name": "versao_motor",
     "description": "Versão do motor oficial e da base de tabelas que está respondendo.",
     "inputSchema": {"type": "object", "properties": {}},
     "annotations": {"readOnlyHint": True, "openWorldHint": False}},
]

# chamar(metodo, caminho_api, corpo_bytes | None) -> (status, dict | list | None)
Chamador = Callable[[str, str, bytes | None], tuple[int, object]]


class ErroFerramenta(Exception):
    pass


def _data(args: dict) -> str:
    d = str(args.get("data", ""))
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
        raise ErroFerramenta("data deve ser AAAA-MM-DD")
    return d


def _inteiro(args: dict, chave: str) -> int | None:
    v = args.get(chave)
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
        raise ErroFerramenta(f"{chave} deve ser um inteiro positivo")
    return v


def executar(nome: str, args: dict, chamar: Chamador) -> object:
    base = "/calculadora/dados-abertos"
    if nome == "calcular_cbs_ibs":
        op = args.get("operacao")
        if not isinstance(op, dict):
            raise ErroFerramenta("operacao deve ser um objeto")
        status, dados = chamar("POST", "/calculadora/observabilidade/regime-geral",
                               json.dumps(op, ensure_ascii=False).encode())
    elif nome == "tabelas_cbs_ibs":
        d = _data(args)
        s1, sit = chamar("GET", f"{base}/situacoes-tributarias/cbs-ibs?data={d}", None)
        status, cls = chamar("GET", f"{base}/classificacoes-tributarias/cbs-ibs?data={d}", None)
        if s1 != 200:
            status = s1
        dados = {"situacoes": sit, "classificacoes": cls}
    elif nome == "aliquotas_referencia":
        d, uf, mun = _data(args), _inteiro(args, "codigo_uf"), _inteiro(args, "codigo_municipio")
        dados, status = {}, 200
        pedidos = [("cbs", f"{base}/aliquota-uniao?data={d}")]
        if uf:
            pedidos.append(("ibs_estadual", f"{base}/aliquota-uf?codigoUf={uf}&data={d}"))
        if mun:
            pedidos.append(("ibs_municipal", f"{base}/aliquota-municipio?codigoMunicipio={mun}&data={d}"))
        for chave, caminho in pedidos:
            s, r = chamar("GET", caminho, None)
            dados[chave] = r.get("aliquotaReferencia") if s == 200 and isinstance(r, dict) else None
        dados["observacao"] = "null = sem alíquota de referência publicada para a data (o motor só traz as de 2026)"
    elif nome == "versao_motor":
        status, dados = chamar("GET", f"{base}/versao", None)
    else:
        raise ErroFerramenta(f"ferramenta {nome!r} não existe")
    if status >= 500:
        raise ErroFerramenta("o motor de cálculo não respondeu; tente de novo em instantes")
    if status >= 400 and not (isinstance(dados, dict) and dados.get("objetos")):
        raise ErroFerramenta(json.dumps(dados, ensure_ascii=False)[:2000])
    return dados


def responder(corpo: object, chamar: Chamador) -> tuple[int, object]:
    """(status HTTP, corpo JSON ou None)."""
    if isinstance(corpo, list):
        if not corpo or len(corpo) > MAX_LOTE or any(isinstance(m, list) for m in corpo):
            return 400, {"jsonrpc": "2.0", "id": None,
                         "error": {"code": -32600, "message": f"lote inválido: de 1 a {MAX_LOTE} itens, sem listas aninhadas"}}
        resp = [r for r in (responder(m, chamar)[1] for m in corpo) if r is not None]
        return (200, resp) if resp else (202, None)
    if not isinstance(corpo, dict) or corpo.get("jsonrpc") != "2.0":
        return 400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "requisição JSON-RPC 2.0 inválida"}}
    m, rid, p = corpo.get("method", ""), corpo.get("id"), corpo.get("params") or {}
    if rid is None and str(m).startswith("notifications/"):
        return 202, None

    def ok(r):
        return 200, {"jsonrpc": "2.0", "id": rid, "result": r}

    if m == "initialize":
        v = p.get("protocolVersion") if isinstance(p, dict) else None
        return ok({"protocolVersion": v if v in VERSOES else VERSOES[0], "capabilities": {"tools": {"listChanged": False}},
                   "serverInfo": {"name": "nota-aberta", "version": "1.0.0"}, "instructions": INSTRUCOES})
    if m == "ping":
        return ok({})
    if m == "tools/list":
        return ok({"tools": FERRAMENTAS})
    if m == "tools/call":
        args = p.get("arguments") or {} if isinstance(p, dict) else {}
        try:
            if not isinstance(args, dict):
                raise ErroFerramenta("arguments deve ser um objeto")
            dados = executar(str(p.get("name", "")), args, chamar)
            texto = json.dumps(dados, ensure_ascii=False)
            res = {"content": [{"type": "text", "text": texto}]}
            if isinstance(dados, dict):
                res["structuredContent"] = dados
            return ok(res)
        except ErroFerramenta as e:
            return ok({"isError": True, "content": [{"type": "text", "text": str(e)}]})
    return 200, {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"método {m!r} não suportado"}}
