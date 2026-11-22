"""Documentação da API e do MCP, gerada do código (página /docs e /llms.txt).

Nasceu de um pedido no X (21/11/2026): "tentei acessar o MCP e não deu. E onde está a documentação da API?". O /mcp só
responde a POST (JSON-RPC), então quem abria no navegador via {"erro": "não encontrado"}, e não havia página de API.

Uma fonte só: as ferramentas saem de `mcp.FERRAMENTAS` e `mcp.CONTA_FERRAMENTAS` (o que o servidor MCP de fato
expõe) e as rotas REST de `ROTAS` abaixo, que `tests/test_docs.py` confere contra o roteador de verdade. Documentação
escrita à mão envelhece em silêncio; esta quebra o teste.
"""
from __future__ import annotations

import html
import json

import mcp

# (método, caminho, o que faz). Toda rota aceita `Authorization: Bearer na_...` (chave criada no painel).
ROTAS = [
    ("GET", "/conta/eu", "Sua conta: empresas, plano e chaves de API."),
    ("POST", "/conta/empresas", ("Cadastra uma empresa: cnpj, razao, municipio_ibge, regime (simples, mei ou normal), "
                                 "inscricao_municipal e regime_especial (opcionais).")),
    ("PATCH", "/conta/empresas/{empresa_id}", "Edita inscricao_municipal e regime_especial da empresa."),
    ("POST", "/conta/empresas/{empresa_id}/certificado", "Sobe o certificado A1: pfx_base64 e senha. Fica cifrado."),
    ("DELETE", "/conta/empresas/{empresa_id}/certificado", "Apaga o certificado do cofre."),
    ("POST", "/conta/empresas/{empresa_id}/notas/conferir", ("Confere a NFS-e sem emitir: devolve pronto, "
                                                              "faltando e perguntar. Não gasta cota nem fala com o governo.")),
    ("POST", "/conta/empresas/{empresa_id}/notas", "Emite a NFS-e. Mesmo `ref` nunca gera segunda nota."),
    ("GET", "/conta/empresas/{empresa_id}/notas", "Lista as NFS-e com status, chave, erros explicados e tributos."),
    ("POST", "/conta/empresas/{empresa_id}/notas/{nota_id}/cancelar", "Cancela a NFS-e (evento 101101): motivo_codigo e motivo."),
    ("GET", "/conta/empresas/{empresa_id}/notas/{nota_id}/pdf", "PDF oficial da NFS-e (DANFSe da Receita)."),
    ("GET", "/conta/empresas/{empresa_id}/nfe/cadastro", "Cadastro da empresa para NF-e de produto (IE, endereço)."),
    ("PUT", "/conta/empresas/{empresa_id}/nfe/cadastro", "Grava o cadastro de NF-e."),
    ("POST", "/conta/empresas/{empresa_id}/nfe", "Emite a NF-e (modelo 55) de produto, Simples Nacional ou MEI."),
    ("GET", "/conta/empresas/{empresa_id}/nfe", "Lista as NF-e."),
    ("GET", "/conta/empresas/{empresa_id}/nfe/{chave}/xml", "XML autorizado da NF-e."),
    ("GET", "/conta/empresas/{empresa_id}/nfe/{chave}/pdf", "DANFE em PDF."),
]

CALCULADORA = [
    ("POST", "/api/calculadora/regime-geral", "Calcula CBS, IBS e IS de uma operação no motor oficial da Receita."),
    ("GET", "/api/calculadora/dados-abertos/...", "Tabelas oficiais: classificações, alíquotas de referência, versão."),
    ("GET", "/api/calculadora/nfse/situacoes-classificacoes-tributarias?nbs=...&data=...",
     "Classificação CBS/IBS de um NBS (código de serviço)."),
    ("GET", "/api/swagger-ui.html", "Swagger do motor oficial, com todas as rotas de cálculo e exemplos."),
]


def _parametros(schema: dict) -> list[tuple[str, str, bool]]:
    props, obrig = schema.get("properties", {}) or {}, set(schema.get("required", []) or [])
    out = []
    for nome, p in props.items():
        tipo = p.get("type") or ("oneOf" if "oneOf" in p else "")
        desc = p.get("description", "")
        if p.get("enum"):
            desc = (desc + " " if desc else "") + "Valores: " + ", ".join(map(str, p["enum"])) + "."
        out.append((nome, f"{tipo}. {desc}".strip(". ").strip() if desc else str(tipo), nome in obrig))
    return out


def ferramentas() -> list[dict]:
    """Cada ferramenta do MCP com o que a página precisa: precisa de chave? escreve?"""
    out = []
    for f, de_conta in [(f, False) for f in mcp.FERRAMENTAS] + [(f, True) for f in mcp.CONTA_FERRAMENTAS]:
        out.append({"nome": f["name"], "descricao": f.get("description", ""), "chave": de_conta,
                    "escreve": not (f.get("annotations") or {}).get("readOnlyHint", False),
                    "parametros": _parametros(f.get("inputSchema") or {})})
    return out


def _config_json(base: str) -> str:
    return json.dumps({"mcpServers": {"nota-aberta": {"type": "http", "url": f"{base}/mcp",
                                                      "headers": {"Authorization": "Bearer na_SUA_CHAVE"}}}},
                      indent=2, ensure_ascii=False)


def markdown(base: str) -> str:
    """/llms.txt: a mesma documentação em texto, para agentes de IA lerem."""
    linhas = [
        "# Nota Aberta: API e MCP", "",
        "> Emissor grátis de NFS-e nacional e NF-e (produto), com CBS e IBS da Reforma pelo motor oficial da Receita.",
        "", f"Base: {base}", "", "## MCP", "",
        f"- URL: {base}/mcp (Streamable HTTP: POST com JSON-RPC 2.0; abrir no navegador mostra esta documentação)",
        ("- Ferramentas de cálculo funcionam sem chave. As de conta pedem `Authorization: Bearer na_...`, criada no "
         f"painel ({base}/painel.html, em Chaves de API)."),
        (f"- Claude Code: `claude mcp add --transport http nota-aberta {base}/mcp --header \"Authorization: Bearer "
         "na_SUA_CHAVE\"`"), "",
        "### Ferramentas", ""]
    for f in ferramentas():
        tags = ("precisa de chave" if f["chave"] else "sem chave") + (", escreve" if f["escreve"] else ", só leitura")
        linhas.append(f"- `{f['nome']}` ({tags}): {f['descricao']}")
        for nome, desc, obrig in f["parametros"]:
            linhas.append(f"  - `{nome}`{' (obrigatório)' if obrig else ''}: {desc}")
    linhas += ["", "## API REST (conta)", "", ("Autenticação: `Authorization: Bearer na_...`. Erro: "
               "`{\"erro\": {\"codigo\": ..., \"mensagem\": ...}}`. Nota recusada traz `erros` com `mensagem` e `dica`."), ""]
    linhas += [f"- `{m} {c}`: {d}" for m, c, d in ROTAS]
    linhas += ["", "## Calculadora (motor oficial, sem chave)", ""]
    linhas += [f"- `{m} {c}`: {d}" for m, c, d in CALCULADORA]
    return "\n".join(linhas) + "\n"


def pagina(base: str) -> str:
    e = html.escape
    fs = ferramentas()

    def ferramenta(f: dict) -> str:
        params = "".join(f"<li><code>{e(n)}</code>{' <em>obrigatório</em>' if o else ''} {e(d)}</li>"
                         for n, d, o in f["parametros"]) or "<li>sem parâmetros</li>"
        etiquetas = (f'<span class="etiqueta">{"precisa de chave" if f["chave"] else "sem chave"}</span> '
                     f'<span class="etiqueta">{"escreve" if f["escreve"] else "só leitura"}</span>')
        return (f'<details class="ferramenta" id="f-{e(f["nome"])}"><summary><code>{e(f["nome"])}</code> {etiquetas}'
                f'</summary><p>{e(f["descricao"])}</p><ul>{params}</ul></details>')

    def tabela(rotas) -> str:
        return ("<table class=\"rotas\"><thead><tr><th>Método</th><th>Caminho</th><th>O que faz</th></tr></thead><tbody>"
                + "".join(f"<tr><td><code>{e(m)}</code></td><td><code>{e(c)}</code></td><td>{e(d)}</td></tr>"
                          for m, c, d in rotas) + "</tbody></table>")

    curl_conferir = (f"curl -X POST {base}/conta/empresas/EMPRESA_ID/notas/conferir \\\n"
                     "  -H \"Authorization: Bearer na_SUA_CHAVE\" -H \"Content-Type: application/json\" \\\n"
                     "  -d '{\"codigo_servico\":\"010101\",\"descricao\":\"Site\",\"valor\":\"1500,00\"}'")
    curl_emitir = (f"curl -X POST {base}/conta/empresas/EMPRESA_ID/notas \\\n"
                   "  -H \"Authorization: Bearer na_SUA_CHAVE\" -H \"Content-Type: application/json\" \\\n"
                   "  -d '{\"codigo_servico\":\"010101\",\"descricao\":\"Site\",\"valor\":\"1500,00\",\n"
                   "       \"ambiente\":\"teste\",\"ref\":\"pedido-42\",\n"
                   "       \"tomador\":{\"documento\":\"11222333000181\",\"nome\":\"Cliente Ltda\"}}'")
    return f"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>API e MCP · Nota Aberta</title>
<meta name="description" content="Documentação da API e do MCP da Nota Aberta: emitir NFS-e e NF-e e calcular CBS e IBS por código ou por agente de IA.">
<meta name="robots" content="index, follow">
<link rel="canonical" href="{e(base)}/docs">
<meta property="og:title" content="API e MCP da Nota Aberta">
<meta property="og:description" content="Emita NFS-e e NF-e e calcule CBS e IBS por código ou por agente de IA. Grátis.">
<meta property="og:image" content="{e(base)}/img/oficina-1920.webp">
<meta name="twitter:card" content="summary_large_image">
<link rel="stylesheet" href="estilo.css">
<link rel="stylesheet" href="docs.css">
</head>
<body>
<main class="docs">
  <p><a href="/">Nota Aberta</a> · <a href="/painel.html">Painel</a> · <a href="/llms.txt">llms.txt</a></p>
  <h1>API e MCP</h1>
  <p>Tudo o que a tela faz, um sistema ou um agente de IA também faz: emitir, conferir, listar e cancelar NFS-e, emitir
  NF-e e calcular CBS e IBS no motor oficial da Receita. Grátis.</p>

  <h2 id="comecar">Começar em 3 passos</h2>
  <ol>
    <li>Entre no <a href="/painel.html">painel</a> com o Google, cadastre a empresa e suba o certificado A1.</li>
    <li>No painel, em <strong>Chaves de API</strong>, crie uma chave (começa com <code>na_</code>). Guarde: ela não aparece de novo.</li>
    <li>Use a chave no cabeçalho <code>Authorization: Bearer na_...</code>, na API ou no MCP.</li>
  </ol>

  <h2 id="mcp">MCP (agentes de IA)</h2>
  <p>Endereço: <code>{e(base)}/mcp</code>. Transporte <em>Streamable HTTP</em>: o agente faz <strong>POST</strong> com
  JSON-RPC 2.0. Abrir o endereço no navegador traz você para esta página; isso é normal.</p>
  <p>As ferramentas de cálculo funcionam sem chave; as de conta (emitir, listar, cancelar) pedem a chave.</p>
  <h3>Claude Code</h3>
  <pre><code>claude mcp add --transport http nota-aberta {e(base)}/mcp \\
  --header "Authorization: Bearer na_SUA_CHAVE"</code></pre>
  <h3>Cursor, Claude Desktop e outros (JSON)</h3>
  <pre><code>{e(_config_json(base))}</code></pre>
  <h3>Teste rápido</h3>
  <pre><code>curl -X POST {e(base)}/mcp -H "Content-Type: application/json" \\
  -d '{{"jsonrpc":"2.0","id":1,"method":"tools/list"}}'</code></pre>

  <h3 id="ferramentas">Ferramentas ({len(fs)})</h3>
  {"".join(ferramenta(f) for f in fs)}

  <h2 id="api">API REST</h2>
  <p>Base <code>{e(base)}</code>. Corpo e resposta em JSON. Erro vem como
  <code>{{"erro": {{"codigo": "...", "mensagem": "..."}}}}</code>; nota recusada traz <code>erros</code> com
  <code>mensagem</code> (o que aconteceu) e <code>dica</code> (o que fazer).</p>
  <h3>Conferir antes de emitir</h3>
  <pre><code>{e(curl_conferir)}</code></pre>
  <h3>Emitir</h3>
  <pre><code>{e(curl_emitir)}</code></pre>
  <p>Resposta: <code>201</code> com <code>status: "emitida"</code>, <code>chave</code> e <code>numero_nfse</code>;
  <code>202</code> com <code>status: "recusada"</code> (com <code>erros</code>) ou <code>"desconhecida"</code> (o governo
  não respondeu: chame de novo com o mesmo <code>ref</code>, que ele consulta antes de reenviar).</p>
  <h3>Rotas</h3>
  <div class="rolar">{tabela(ROTAS)}</div>

  <h2 id="calculadora">Calculadora da Reforma (sem chave)</h2>
  <p>O motor oficial da Receita Federal (MIT), sem alteração, atrás de um teto de pedidos por IP.</p>
  <div class="rolar">{tabela(CALCULADORA)}</div>

  <p class="rodape-docs">Esta página é gerada do código do servidor: a lista de ferramentas é a mesma que o MCP expõe.
  Código aberto (MIT) da <a href="https://genesisinnovation.io">Genesis Innovation</a>.</p>
</main>
</body>
</html>
"""
