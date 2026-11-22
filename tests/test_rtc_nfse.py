"""Classificação de IBS/CBS pelo NBS: o motor oficial decide, e ambiguidade volta para quem emite."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import contas
import rtc_nfse

# Respostas reais do motor (release 1.6.1, 20/11/2026) para o NBS 115021000.
CLASSE = {"codigoSituacaoTributaria": "000", "descricaoSituacaoTributaria": "Tributação integral",
          "codigoClassificacaoTributaria": "000001",
          "descricaoClassificacaoTributaria": "Situações tributadas integralmente pelo IBS e CBS."}
INDICADOR = {"cIndOp": "100301", "tipoOperacao": "Demais serviços, em operações onerosas",
             "localIncidencia": "Endereço do Tomador/Adquirente"}


def motor(classes, indicadores, status=200):
    chamadas = []

    def chamar(metodo, api, dados):
        chamadas.append(api)
        if status != 200:
            return status, None
        return 200, classes if "situacoes" in api else indicadores
    chamar.chamadas = chamadas
    return chamar


def test_nbs_com_uma_opcao_so_vira_cst_cclasstrib_e_cindop_do_motor():
    chamar = motor([CLASSE], [INDICADOR])
    assert rtc_nfse.classificar("115021000", "2026-11-20", chamar=chamar) == {
        "cst": "000", "cclass_trib": "000001", "cind_op": "100301"}
    assert any("nbs=115021000" in c and "data=2026-11-20" in c for c in chamar.chamadas)


def test_nbs_com_mais_de_uma_classificacao_nao_escolhe_sozinho():
    outra = CLASSE | {"codigoSituacaoTributaria": "200", "codigoClassificacaoTributaria": "200001"}
    with pytest.raises(contas.Erro) as e:
        rtc_nfse.classificar("115021000", "2026-11-20", chamar=motor([CLASSE, outra], [INDICADOR]))
    assert e.value.status == 422 and e.value.codigo == "classificacao_ambigua" and "200001" in e.value.mensagem


def test_escolha_de_quem_emite_desempata_a_ambiguidade():
    outra = CLASSE | {"codigoSituacaoTributaria": "200", "codigoClassificacaoTributaria": "200001"}
    r = rtc_nfse.classificar("115021000", "2026-11-20", {"cclass_trib": "200001"}, chamar=motor([CLASSE, outra], [INDICADOR]))
    assert r == {"cst": "200", "cclass_trib": "200001", "cind_op": "100301"}


def test_escolha_completa_nao_pergunta_ao_motor():
    chamar = motor([], [])
    escolha = {"cst": "000", "cclass_trib": "000001", "cind_op": "100301"}
    assert rtc_nfse.classificar("", "2026-11-20", escolha, chamar=chamar) == escolha and chamar.chamadas == []


def test_nbs_que_o_motor_nao_conhece_e_422_e_motor_fora_e_503():
    with pytest.raises(contas.Erro) as e:
        rtc_nfse.classificar("999999999", "2026-11-20", chamar=motor([], [INDICADOR]))
    assert e.value.codigo == "nbs_sem_classificacao"
    with pytest.raises(contas.Erro) as e:
        rtc_nfse.classificar("115021000", "2026-11-20", chamar=motor([], [], status=502))
    assert e.value.status == 503
