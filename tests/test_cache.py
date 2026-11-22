"""Cache em camadas: CSS/JS imutável só com o hash certo, página curta na borda, API GET de dados abertos cacheável e
cálculo repetido servido da memória sem tocar no motor oficial."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

AQUI = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AQUI))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_servidor import MotorFalso, _req, porta  # noqa: F401 (fixture reaproveitada)

import servidor

CALC = "/api/calculadora/regime-geral"


def test_css_com_hash_do_conteudo_e_imutavel_e_sem_hash_ou_velho_revalida(porta):  # noqa: F811
    _, html, _ = _req(porta + "/")
    v = re.search(rb"landing\.css\?v=([0-9a-f]{10})", html).group(1).decode()
    assert "immutable" in _req(porta + f"/landing.css?v={v}")[2]["Cache-Control"]
    assert _req(porta + "/landing.css")[2]["Cache-Control"] == "no-cache"
    assert _req(porta + "/landing.css?v=0000000000")[2]["Cache-Control"] == "no-cache"


def test_pagina_fica_pouco_na_borda_e_operacao_nunca(porta):  # noqa: F811
    cc = _req(porta + "/")[2]["Cache-Control"]
    assert "s-maxage=300" in cc and "stale-while-revalidate" in cc
    assert "no-store" in _req(porta + "/operacao")[2]["Cache-Control"]


def test_dados_abertos_do_motor_saem_cacheaveis_na_borda(porta):  # noqa: F811
    st, _, cab = _req(porta + "/api/calculadora/dados-abertos/versao")
    assert st == 200 and "s-maxage" in cab["Cache-Control"]


def test_calculo_repetido_vem_da_memoria_sem_chamar_o_motor_de_novo(porta):  # noqa: F811
    servidor.MEMORIA.itens.clear()
    servidor.MEMORIA.bytes = 0
    corpo = json.dumps({"id": "x", "itens": [1]}).encode()
    cab = {"Content-Type": "application/json"}
    _, primeiro, h1 = _req(porta + CALC, "POST", corpo, cab)
    _, segundo, h2 = _req(porta + CALC, "POST", corpo, cab)
    assert (h1["X-Cache"], h2["X-Cache"]) == ("MISS", "HIT") and primeiro == segundo
    assert sum(1 for v in MotorFalso.vistos if v[0] == "POST") == 1
    _req(porta + CALC, "POST", json.dumps({"id": "y"}).encode(), cab)  # corpo diferente vai ao motor
    assert sum(1 for v in MotorFalso.vistos if v[0] == "POST") == 2


def test_memoria_expira_no_prazo_e_respeita_o_teto_de_bytes():
    t = [0.0]
    m = servidor.MemoriaDeCalculo(segundos=10, teto_bytes=80, relogio=lambda: t[0])
    m.guardar("a", b"1" * 10, "x")
    t[0] = 11
    assert m.ler("a") is None and m.bytes == 0
    for i in range(10):
        m.guardar(str(i), b"2" * 10, "x")
    assert m.bytes <= 80 and m.ler("0") is None and m.ler("9") is not None
    m.guardar("grande", b"3" * 50, "x")  # acima de 1/8 do teto: não guarda
    assert m.ler("grande") is None
