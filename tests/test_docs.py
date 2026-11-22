"""Documentação da API e do MCP (/docs, /llms.txt) e o GET /mcp que antes dava 404 no navegador (X, 21/11/2026)."""
from __future__ import annotations

import http.client
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

AQUI = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AQUI))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_conta_web import _req, app  # noqa: F401 (fixture reaproveitada)

import docs
import mcp


def _cru(url: str, caminho: str, cab: dict | None = None):
    """Sem seguir redirecionamento, para ver o 302 e o Location."""
    u = urlparse(url)
    c = http.client.HTTPConnection(u.hostname, u.port, timeout=5)
    c.request("GET", caminho, headers=cab or {})
    r = c.getresponse()
    corpo = r.read()
    c.close()
    return r.status, dict(r.getheaders()), corpo


def test_toda_ferramenta_do_mcp_aparece_na_documentacao():
    nomes = {f["name"] for f in mcp.FERRAMENTAS + mcp.CONTA_FERRAMENTAS}
    pagina, texto = docs.pagina("https://x"), docs.markdown("https://x")
    for n in nomes:
        assert f'id="f-{n}"' in pagina and f"`{n}`" in texto


def test_toda_rota_rest_documentada_existe_no_roteador(app):  # noqa: F811
    # Documentação escrita à mão envelhece em silêncio: se uma rota sair do conta_web, este teste quebra.
    cab = {"Authorization": f"Bearer {app['chave']}"}
    # Empresa de verdade: com id falso o roteador responde "empresa não encontrada" antes de olhar a rota.
    eid = app["c"].cadastrar_empresa(app["u"]["id"], "11222333000181", "Empresa", 4205407, "simples")["id"]
    for metodo, caminho, _ in docs.ROTAS:
        url = app["url"] + re.sub(r"\{[a-z_]+\}", "x", caminho.replace("{empresa_id}", eid))
        corpo = _req(url, metodo, {} if metodo != "GET" else None, cab)[1]
        codigo = (corpo.get("erro") or {}).get("codigo") if isinstance(corpo, dict) else None
        assert codigo != "rota_inexistente", f"{metodo} {caminho} documentada mas o roteador não conhece"


def test_mcp_aberto_no_navegador_leva_a_documentacao_e_nao_a_um_404(app):  # noqa: F811
    st, cab, _ = _cru(app["url"], "/mcp", {"Accept": "text/html,application/xhtml+xml"})
    assert st == 302 and cab["Location"] == "/docs#mcp"


def test_cliente_mcp_pedindo_sse_recebe_405_como_manda_a_spec_streamable_http(app):  # noqa: F811
    st, cab, _ = _cru(app["url"], "/mcp", {"Accept": "text/event-stream"})
    assert st == 405 and cab["Allow"] == "POST"


def test_api_e_api_docs_levam_a_pagina_de_documentacao(app):  # noqa: F811
    for caminho in ("/api", "/api/", "/api/docs"):
        st, cab, _ = _cru(app["url"], caminho)
        assert st == 302 and cab["Location"] == "/docs", caminho


def test_pagina_de_documentacao_abre_com_csp_e_mostra_como_conectar(app):  # noqa: F811
    st, cab, corpo = _cru(app["url"], "/docs")
    html = corpo.decode()
    assert st == 200 and "Content-Security-Policy" in cab
    assert "claude mcp add --transport http" in html and "Authorization: Bearer na_" in html
    assert "<script" not in html  # a CSP estrita da página proíbe script inline


def test_llms_txt_responde_texto_para_agentes(app):  # noqa: F811
    st, cab, corpo = _cru(app["url"], "/llms.txt")
    assert st == 200 and cab["Content-Type"].startswith("text/plain") and b"emitir_nfse" in corpo
