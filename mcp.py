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
    {"name": "classificar_nbs",
     "description": "Para um NBS e uma data, as classificações de IBS/CBS (CST e cClassTrib) e os indicadores de operação "
                    "(cIndOp, com o local de incidência) que o motor oficial aceita na NFS-e. Use antes de emitir_nfse "
                    "quando o NBS tiver mais de uma opção.",
     "inputSchema": {"type": "object", "required": ["nbs", "data"],
                     "properties": {"nbs": {"type": "string", "pattern": r"^\d{9}$"}, "data": DATA}},
     "annotations": {"readOnlyHint": True, "openWorldHint": False}},
    {"name": "versao_motor",
     "description": "Versão do motor oficial e da base de tabelas que está respondendo.",
     "inputSchema": {"type": "object", "properties": {}},
     "annotations": {"readOnlyHint": True, "openWorldHint": False}},
]

CONTA_FERRAMENTAS = [
    {"name": "listar_empresas",
     "description": "Empresas da conta dona da chave, com certificado (validade), notas no mês, inscrição municipal, "
                    "regime especial de tributação (regEspTrib) e o plano com a cota.",
     "inputSchema": {"type": "object", "properties": {}},
     "annotations": {"readOnlyHint": True, "openWorldHint": False}},
    {"name": "editar_empresa",
     "description": "Muda a inscrição municipal (IM do prestador na NFS-e, 1 a 15 letras ou dígitos; vazio apaga) e o "
                    "regime especial de tributação do ISS (regEspTrib) da empresa. Só esses dois campos; omitido = não muda.",
     "inputSchema": {"type": "object", "required": ["empresa_id"],
                     "properties": {"empresa_id": {"type": "string"},
                                    "inscricao_municipal": {"type": "string"},
                                    "regime_especial": {"type": "string", "enum": ["0", "1", "2", "3", "4", "5", "6", "9"],
                                                        "description": "0 nenhum, 1 ato cooperado (cooperativa), 2 estimativa, "
                                                                       "3 microempresa municipal, 4 notário ou registrador, "
                                                                       "5 profissional autônomo, 6 sociedade de profissionais, "
                                                                       "9 outros"}}},
     "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}},
    {"name": "conferir_nfse",
     "description": "Confere uma NFS-e ANTES de emitir, sem gastar cota e sem falar com o governo: devolve `pronto`, "
                    "`faltando` (campos que impedem a emissão, todos de uma vez) e `perguntar` (campos que mudam a nota, "
                    "como a alíquota do Simples e o ambiente). Mesmos argumentos do emitir_nfse. Se `faltando` ou "
                    "`perguntar` vierem com itens, pergunte ao usuário e só depois chame emitir_nfse.",
     "inputSchema": None,  # o mesmo do emitir_nfse, preenchido abaixo da lista: uma definição só
     "annotations": {"readOnlyHint": True, "openWorldHint": False}},
    {"name": "emitir_nfse",
     "description": "Emite uma NFS-e nacional pela empresa, com o certificado A1 guardado. Chame conferir_nfse antes. "
                    "Use `ref` (seu id do pedido): "
                    "repetir o mesmo ref nunca gera segunda nota. Status: emitida, recusada (com erros do governo; não "
                    "gasta cota) ou desconhecida (sem resposta do governo; chame de novo com o mesmo ref, que ele consulta "
                    "antes de reenviar). `ambiente`: teste (padrão, sem valor fiscal) ou producao (nota com valor fiscal).",
     "inputSchema": {"type": "object", "required": ["empresa_id", "codigo_servico", "descricao", "valor"],
                     "properties": {"empresa_id": {"type": "string"},
                                    "codigo_servico": {"type": "string", "description": "cTribNac, 6 dígitos, ex.: 010101"},
                                    "nbs": {"type": "string", "description": "NBS, 9 dígitos. Com ele, a nota sai com o grupo IBS/CBS "
                                                                            "da Reforma classificado pelo motor oficial (veja classificar_nbs)"},
                                    "descricao": {"type": "string"},
                                    "valor": {"type": "string", "description": "em reais, ex.: 1500.00"},
                                    "competencia": {"type": "string", "description": "AAAA-MM-DD; padrão: hoje"},
                                    "ref": {"type": "string"},
                                    "ambiente": {"type": "string", "enum": ["teste", "producao"],
                                                 "description": "teste (padrão, sem valor fiscal) ou producao (valor fiscal)"},
                                    "ibscbs": {"description": "false = sem o grupo IBS/CBS; ou objeto {cst, cclass_trib, "
                                                              "cind_op, ind_final} para escolher quando o NBS tem mais de "
                                                              "uma classificação. Omita para o motor oficial classificar.",
                                               "oneOf": [{"type": "boolean"}, {"type": "object"}]},
                                    "aliquota_simples": {"type": "string", "description": "% aproximado dos tributos do "
                                                         "Simples Nacional (pTotTribSN), ex.: 6.00; só ME/EPP do Simples; "
                                                         "vazio = 0"},
                                    "retencoes": {"type": "object", "description": "retenções na fonte (Lucro Real/Presumido, "
                                                  "órgão público). Valores em reais.",
                                                  "properties": {"iss_retido": {"type": "boolean"},
                                                                 "aliquota_iss": {"type": "string", "description": "% do ISS, ex.: 2.00; obrigatória com ISS retido no Simples"},
                                                                 "irrf": {"type": "string"}, "csll": {"type": "string"},
                                                                 "inss": {"type": "string", "description": "contribuição previdenciária retida"},
                                                                 "pis": {"type": "string"}, "cofins": {"type": "string"},
                                                                 "pis_cofins_retidos": {"type": "boolean"}}},
                                    "tomador": {"type": "object", "properties": {"documento": {"type": "string"},
                                                                                 "nome": {"type": "string"},
                                                                                 "email": {"type": "string"},
                                                                                 "cep": {"type": "string"},
                                                                                 "municipio_ibge": {"type": "string", "description": "7 dígitos"},
                                                                                 "logradouro": {"type": "string"},
                                                                                 "numero": {"type": "string"},
                                                                                 "complemento": {"type": "string"},
                                                                                 "bairro": {"type": "string"}}}}},
     "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True}},
    {"name": "cancelar_nfse",
     "description": "Cancela uma NFS-e emitida (evento 101101 no governo, no mesmo ambiente em que ela saiu). motivo_codigo: "
                    "1 erro na emissão, 2 serviço não prestado, 9 outro; motivo com 15 caracteres ou mais. Não tem volta.",
     "inputSchema": {"type": "object", "required": ["empresa_id", "nota_id", "motivo"],
                     "properties": {"empresa_id": {"type": "string"}, "nota_id": {"type": "string"},
                                    "motivo_codigo": {"type": "string", "enum": ["1", "2", "9"]},
                                    "motivo": {"type": "string"}}},
     "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": True}},
    {"name": "cadastro_nfe",
     "description": "Cadastro da NF-e de produto de uma empresa: ver (sem `dados`) ou salvar (com `dados`: ie, logradouro, "
                    "numero, complemento, bairro, cep, municipio, telefone, fantasia, serie_teste, serie_producao, "
                    "proximo_teste, proximo_producao). A UF sai do município da empresa. Próximo número só sobe (para "
                    "continuar a numeração de outro sistema).",
     "inputSchema": {"type": "object", "required": ["empresa_id"],
                     "properties": {"empresa_id": {"type": "string"}, "dados": {"type": "object"}}},
     "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}},
    {"name": "emitir_nfe",
     "description": "Emite uma NF-e de produto (modelo 55) pela SEFAZ da UF da empresa, para Simples Nacional e MEI. "
                    "`ref` evita nota dupla. Status: emitida, recusada (erro da SEFAZ; não gasta número nem cota), "
                    "denegada ou desconhecida (sem resposta; chame de novo com o mesmo ref, que ele consulta a chave antes). "
                    "`ambiente`: teste (padrão, homologação, sem valor fiscal) ou producao.",
     "inputSchema": {"type": "object", "required": ["empresa_id", "destinatario", "itens"],
                     "properties": {"empresa_id": {"type": "string"}, "ref": {"type": "string"},
                                    "ambiente": {"type": "string", "enum": ["teste", "producao"]},
                                    "destinatario": {"type": "object", "description": "documento (CPF/CNPJ), nome, ie (vazio = "
                                                     "não contribuinte), email, cep, logradouro, numero, complemento, bairro, "
                                                     "municipio_ibge, municipio, uf"},
                                    "itens": {"type": "array", "items": {"type": "object", "description": "codigo, descricao, "
                                              "ncm (8 dígitos), quantidade, valor_unitario, unidade, gtin, cfop (vazio = "
                                              "5102/6102), csosn (102, 103, 300, 400, 500), origem, cest"}},
                                    "frete": {"type": "string"}, "presencial": {"type": "boolean"},
                                    "forma_pagamento": {"type": "string", "description": "pix (padrão), dinheiro, credito, "
                                                        "debito, boleto, deposito, transferencia, outros, sem_pagamento"},
                                    "pagamentos": {"type": "array", "items": {"type": "object"}},
                                    "natureza": {"type": "string"}, "informacoes": {"type": "string"}}},
     "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True}},
    {"name": "listar_nfe",
     "description": "Últimas NF-e de produto da empresa, com status, série, número, chave e protocolo.",
     "inputSchema": {"type": "object", "required": ["empresa_id"], "properties": {"empresa_id": {"type": "string"}}},
     "annotations": {"readOnlyHint": True, "openWorldHint": False}},
    {"name": "listar_notas",
     "description": "Últimas notas da empresa, com status, chave de acesso e erros.",
     "inputSchema": {"type": "object", "required": ["empresa_id"], "properties": {"empresa_id": {"type": "string"}}},
     "annotations": {"readOnlyHint": True, "openWorldHint": False}},
]
_POR_NOME = {f["name"]: f for f in CONTA_FERRAMENTAS}
# Conferir aceita o pedido incompleto (é para isso que existe): só a empresa é obrigatória.
_POR_NOME["conferir_nfse"]["inputSchema"] = {**_POR_NOME["emitir_nfse"]["inputSchema"], "required": ["empresa_id"]}
NOMES_CONTA = set(_POR_NOME)

# conta(metodo, caminho, corpo_dict | None) -> (status, dict): a mesma rota /conta da página, com a chave de API.
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


def executar_conta(nome: str, args: dict, conta) -> object:
    if conta is None:
        raise ErroFerramenta("esta ferramenta pede `Authorization: Bearer na_...` (crie a chave no painel)")
    eid = str(args.get("empresa_id", ""))
    if nome == "listar_empresas":
        status, dados = conta("GET", "/conta/eu", None)
        if status == 200:
            dados = {"empresas": dados["empresas"], "plano": dados["plano"], "producao": dados["producao"]}
    elif nome == "editar_empresa":
        status, dados = conta("PATCH", f"/conta/empresas/{eid}",
                              {k: args[k] for k in ("inscricao_municipal", "regime_especial") if k in args})
    elif nome == "conferir_nfse":
        status, dados = conta("POST", f"/conta/empresas/{eid}/notas/conferir", {k: v for k, v in args.items() if k != "empresa_id"})
    elif nome == "emitir_nfse":
        pedido = {k: v for k, v in args.items() if k != "empresa_id"}
        status, dados = conta("POST", f"/conta/empresas/{eid}/notas", pedido)
    elif nome == "cancelar_nfse":
        status, dados = conta("POST", f"/conta/empresas/{eid}/notas/{args.get('nota_id', '')}/cancelar",
                              {"motivo_codigo": str(args.get("motivo_codigo", "1")), "motivo": str(args.get("motivo", ""))})
    elif nome == "cadastro_nfe":
        dados = args.get("dados")
        status, dados = (conta("PUT", f"/conta/empresas/{eid}/nfe/cadastro", dados) if isinstance(dados, dict)
                         else conta("GET", f"/conta/empresas/{eid}/nfe/cadastro", None))
    elif nome == "emitir_nfe":
        status, dados = conta("POST", f"/conta/empresas/{eid}/nfe", {k: v for k, v in args.items() if k != "empresa_id"})
    elif nome == "listar_nfe":
        status, dados = conta("GET", f"/conta/empresas/{eid}/nfe", None)
    elif nome == "listar_notas":
        status, dados = conta("GET", f"/conta/empresas/{eid}/notas", None)
    else:
        raise ErroFerramenta(f"ferramenta {nome!r} não existe")
    if status >= 400:
        raise ErroFerramenta(json.dumps(dados, ensure_ascii=False)[:2000])
    return dados


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
    elif nome == "classificar_nbs":
        nbs = str(args.get("nbs", ""))
        if not re.fullmatch(r"\d{9}", nbs):
            raise ErroFerramenta("nbs tem 9 dígitos")
        import rtc_nfse
        from contas import Erro
        try:
            dados, status = rtc_nfse.opcoes(nbs, _data(args), chamar), 200
        except Erro as e:
            raise ErroFerramenta(e.mensagem) from None
    elif nome == "versao_motor":
        status, dados = chamar("GET", f"{base}/versao", None)
    else:
        raise ErroFerramenta(f"ferramenta {nome!r} não existe")
    if status >= 500:
        raise ErroFerramenta("o motor de cálculo não respondeu; tente de novo em instantes")
    if status >= 400 and not (isinstance(dados, dict) and dados.get("objetos")):
        raise ErroFerramenta(json.dumps(dados, ensure_ascii=False)[:2000])
    return dados


def responder(corpo: object, chamar: Chamador, conta=None) -> tuple[int, object]:
    """(status HTTP, corpo JSON ou None)."""
    if isinstance(corpo, list):
        if not corpo or len(corpo) > MAX_LOTE or any(isinstance(m, list) for m in corpo):
            return 400, {"jsonrpc": "2.0", "id": None,
                         "error": {"code": -32600, "message": f"lote inválido: de 1 a {MAX_LOTE} itens, sem listas aninhadas"}}
        resp = [r for r in (responder(m, chamar, conta)[1] for m in corpo) if r is not None]
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
        return ok({"tools": FERRAMENTAS + CONTA_FERRAMENTAS})
    if m == "tools/call":
        args = p.get("arguments") or {} if isinstance(p, dict) else {}
        try:
            if not isinstance(args, dict):
                raise ErroFerramenta("arguments deve ser um objeto")
            nome = str(p.get("name", ""))
            dados = executar_conta(nome, args, conta) if nome in NOMES_CONTA else executar(nome, args, chamar)
            texto = json.dumps(dados, ensure_ascii=False)
            res = {"content": [{"type": "text", "text": texto}]}
            if isinstance(dados, dict):
                res["structuredContent"] = dados
            return ok(res)
        except ErroFerramenta as e:
            return ok({"isError": True, "content": [{"type": "text", "text": str(e)}]})
    return 200, {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"método {m!r} não suportado"}}
