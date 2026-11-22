"""Indexação no Google e no Bing: robots, sitemap, canonical e a chave do IndexNow saem do próprio servidor.

Nasceu de medição em 21/11/2026: /robots.txt, /sitemap.xml e /favicon.ico davam 404 em produção (97 pedidos de
favicon em 4 h, todos 404) com o site recebendo 30 mil pedidos do lançamento no X."""
from __future__ import annotations

import re
import sys
from pathlib import Path

AQUI = Path(__file__).resolve().parents[1]
WEB = AQUI / "web"
sys.path.insert(0, str(AQUI))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_servidor import _req, porta  # noqa: F401 (fixture reaproveitada)

import docs

BASE = "https://reforma.genesisinnovation.io"


def test_robots_sitemap_favicon_e_chave_indexnow_respondem_200_com_o_tipo_certo(porta):  # noqa: F811
    for caminho, tipo in (("/robots.txt", "text/plain"), ("/sitemap.xml", "application/xml"),
                          ("/favicon.ico", "image/x-icon")):
        st, _, cab = _req(porta + caminho)
        assert st == 200 and cab["Content-Type"].startswith(tipo), caminho
    chaves = list(WEB.glob("nota-aberta-indexnow-*.txt"))
    assert len(chaves) == 1
    st, corpo, _ = _req(porta + "/" + chaves[0].name)
    assert st == 200 and corpo.decode() == chaves[0].stem  # o IndexNow confere que o arquivo traz a própria chave


def test_robots_aponta_o_sitemap_e_fecha_conta_e_painel_de_operacao():
    r = (WEB / "robots.txt").read_text()
    assert f"Sitemap: {BASE}/sitemap.xml" in r
    for fechado in ("/conta/", "/operacao", "/painel.html"):
        assert f"Disallow: {fechado}" in r


def test_toda_url_do_sitemap_existe_e_e_indexavel_com_canonical_nela_mesma():
    urls = re.findall(r"<loc>([^<]+)</loc>", (WEB / "sitemap.xml").read_text())
    assert urls
    for u in urls:
        nome = u.removeprefix(BASE + "/") or "index.html"
        # /docs é gerada pelo servidor (docs.py), não é arquivo em web/
        html = docs.pagina(BASE) if nome == "docs" else (WEB / nome).read_text()
        assert '<meta name="robots" content="index, follow">' in html, nome
        assert f'<link rel="canonical" href="{u}">' in html, nome
        assert re.search(r'<meta property="og:image" content="https://', html), nome  # o X só mostra imagem com URL absoluta


def test_pagina_logada_nao_entra_no_sitemap():
    s = (WEB / "sitemap.xml").read_text()
    assert "painel" not in s and "operacao" not in s and "/conta" not in s
