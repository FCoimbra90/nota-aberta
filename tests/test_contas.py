"""Cofre, certificado e contas. Os certificados são gerados aqui, autoassinados, com o CNPJ no mesmo lugar que um
e-CNPJ ICP-Brasil põe (SAN otherName 2.16.76.1.3.3): nenhum certificado real entra no teste nem no Git."""
from __future__ import annotations

import base64
import datetime as dt
import sys
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import BestAvailableEncryption, pkcs12
from cryptography.x509.oid import NameOID

AQUI = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AQUI))
import certificado
import cofre
import contas

CNPJ = "12345678000195"
MESTRA = bytes(range(32))
SENHA = "senha-de-teste"


def fazer_pfx(cnpj=CNPJ, *, dias=(-1, 365), senha=SENHA, no_san=True, no_cn=True) -> bytes:
    chave = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    cn = f"EMPRESA TESTE LTDA:{cnpj}" if no_cn else "EMPRESA TESTE LTDA"
    nome = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    agora = dt.datetime.now(dt.timezone.utc)
    b = (x509.CertificateBuilder().subject_name(nome).issuer_name(nome).public_key(chave.public_key())
         .serial_number(x509.random_serial_number())
         .not_valid_before(agora + dt.timedelta(days=dias[0])).not_valid_after(agora + dt.timedelta(days=dias[1])))
    if no_san:
        valor = b"\x13\x0e" + cnpj.encode()  # PrintableString com os 14 dígitos, como na ICP-Brasil
        b = b.add_extension(x509.SubjectAlternativeName([x509.OtherName(certificado.OID_CNPJ, valor)]), critical=False)
    cert = b.sign(chave, hashes.SHA256())
    return pkcs12.serialize_key_and_certificates(b"teste", chave, cert, None, BestAvailableEncryption(senha.encode()))


@pytest.fixture()
def c(tmp_path):
    return contas.Contas(tmp_path / "nota.db", b"s" * 32)


def _conta_com_empresa(c):
    u = c.entrar_google({"sub": "g1", "email": "ana@exemplo.com", "verificado": True, "nome": "Ana"})
    e = c.cadastrar_empresa(u["id"], "12.345.678/0001-95", "Empresa Teste", 4205407)
    return u, e


# ------------------------------------------------------------------ cofre

def test_cofre_sem_chave_mestra_recusa_em_vez_de_guardar_em_claro():
    with pytest.raises(cofre.CofreIndisponivel):
        cofre.chave_mestra({})
    with pytest.raises(cofre.CofreIndisponivel):
        cofre.chave_mestra({"NOTA_CHAVE_MESTRA": base64.b64encode(b"curta").decode()})


def test_cofre_nao_deixa_o_pfx_em_claro_e_volta_igual():
    cols = cofre.guardar("emp_a", b"PFX-BYTES-AQUI", "s3nha", MESTRA)
    assert all(b"PFX-BYTES-AQUI" not in v and b"s3nha" not in v for v in cols.values())
    assert cofre.abrir("emp_a", cols, MESTRA) == (b"PFX-BYTES-AQUI", "s3nha")


def test_cofre_pfx_trocado_de_empresa_no_banco_nao_abre():
    cols = cofre.guardar("emp_a", b"pfx", "s", MESTRA)
    with pytest.raises(cofre.CofreViolado):
        cofre.abrir("emp_b", cols, MESTRA)


def test_cofre_chave_mestra_errada_nao_abre():
    cols = cofre.guardar("emp_a", b"pfx", "s", MESTRA)
    with pytest.raises(cofre.CofreViolado):
        cofre.abrir("emp_a", cols, bytes(32))


# ------------------------------------------------------------ certificado

def test_certificado_valido_le_cnpj_do_san():
    info = certificado.ler(fazer_pfx(no_cn=False), SENHA, cnpj_esperado="12.345.678/0001-95")
    assert info["cnpj"] == CNPJ and info["dias_restantes"] >= 363


def test_certificado_sem_san_le_cnpj_do_fim_do_cn():
    assert certificado.ler(fazer_pfx(no_san=False), SENHA, cnpj_esperado=CNPJ)["cnpj"] == CNPJ


def test_certificado_de_outro_cnpj_e_recusado():
    with pytest.raises(certificado.CertificadoRecusado, match="não do"):
        certificado.ler(fazer_pfx("99999999000199"), SENHA, cnpj_esperado=CNPJ)


def test_certificado_vencido_e_recusado():
    with pytest.raises(certificado.CertificadoRecusado, match="venceu"):
        certificado.ler(fazer_pfx(dias=(-400, -1)), SENHA, cnpj_esperado=CNPJ)


def test_certificado_senha_errada_e_recusado_com_mensagem_clara():
    with pytest.raises(certificado.CertificadoRecusado, match="senha"):
        certificado.ler(fazer_pfx(), "outra", cnpj_esperado=CNPJ)


# ----------------------------------------------------------------- contas

def test_entrar_com_email_nao_verificado_e_recusado(c):
    with pytest.raises(contas.Erro) as e:
        c.entrar_google({"sub": "g9", "email": "x@y.com", "verificado": False})
    assert e.value.status == 403


def test_sessao_adulterada_ou_vencida_nao_vale(c):
    u = c.entrar_google({"sub": "g1", "email": "a@b.com", "verificado": True})
    tok = c.emitir_sessao(u["id"], agora=1000)
    assert c.usuario_da_sessao(tok, agora=1001)["id"] == u["id"]
    assert c.usuario_da_sessao(tok, agora=1000 + contas.SESSAO_SEGUNDOS + 1) is None
    corpo, _, ass = tok.rpartition(".")
    assert c.usuario_da_sessao(corpo + "." + "0" * len(ass), agora=1001) is None


def test_cnpj_de_outra_conta_nao_pode_ser_adotado(c):
    _conta_com_empresa(c)
    outro = c.entrar_google({"sub": "g2", "email": "bia@exemplo.com", "verificado": True})
    with pytest.raises(contas.Erro) as e:
        c.cadastrar_empresa(outro["id"], CNPJ, "Outra", 4205407)
    assert e.value.status == 409


def test_empresa_de_outra_conta_e_404_e_nao_revela_que_existe(c):
    _, e = _conta_com_empresa(c)
    outro = c.entrar_google({"sub": "g2", "email": "bia@exemplo.com", "verificado": True})
    with pytest.raises(contas.Erro) as err:
        c.abrir_certificado(outro["id"], e["id"], MESTRA)
    assert err.value.status == 404


def test_certificado_guardado_cifrado_abre_igual_e_apagar_remove_de_verdade(c):
    u, e = _conta_com_empresa(c)
    pfx = fazer_pfx()
    info = c.guardar_certificado(u["id"], e["id"], pfx, SENHA, MESTRA)
    assert info["valido_ate"]
    bruto = c.bd.execute("select pfx_cifrado from certificados").fetchone()[0]
    assert pfx not in bruto
    assert c.abrir_certificado(u["id"], e["id"], MESTRA) == (pfx, SENHA)
    assert c.apagar_certificado(u["id"], e["id"]) is True
    assert c.bd.execute("select count(*) from certificados").fetchone()[0] == 0


def test_certificado_de_outro_cnpj_nao_entra_no_cofre(c):
    u, e = _conta_com_empresa(c)
    with pytest.raises(contas.Erro) as err:
        c.guardar_certificado(u["id"], e["id"], fazer_pfx("99999999000199"), SENHA, MESTRA)
    assert err.value.status == 422
    assert c.bd.execute("select count(*) from certificados").fetchone()[0] == 0


def test_cota_do_mes_para_no_limite_e_devolve_nota_recusada(c):
    _, e = _conta_com_empresa(c)
    assert [c.consumir_nota(e["id"], 2, mes="2027-01") for _ in range(2)] == [1, 2]
    with pytest.raises(contas.Erro) as err:
        c.consumir_nota(e["id"], 2, mes="2027-01")
    assert err.value.status == 402
    c.devolver_nota(e["id"], mes="2027-01")
    assert c.consumir_nota(e["id"], 2, mes="2027-01") == 2


def test_banco_antigo_sem_im_nem_regime_especial_ganha_as_colunas_sem_perder_a_empresa(tmp_path):
    import sqlite3
    caminho = tmp_path / "antigo.db"
    bd = sqlite3.connect(str(caminho))
    bd.executescript("""
    create table usuarios (id text primary key, google_sub text unique not null, email text not null,
      nome text not null default '', criado text not null);
    create table empresas (id text primary key, usuario_id text not null references usuarios(id), cnpj text unique not null,
      razao text not null, municipio_ibge integer not null, regime text not null default 'simples', criado text not null);
    insert into usuarios values ('usu_1', 'g1', 'ana@exemplo.com', 'Ana', '2026-11-01');
    insert into empresas values ('emp_1', 'usu_1', '12345678000195', 'Antiga', 4205407, 'simples', '2026-11-01');
    """)
    bd.commit()
    bd.close()
    c = contas.Contas(caminho, b"s" * 32)
    e = c.empresa("usu_1", "emp_1")
    assert (e["razao"], e["inscricao_municipal"], e["regime_especial"]) == ("Antiga", "", "0")
    contas.Contas(caminho, b"s" * 32)  # abrir de novo não tenta acrescentar a coluna outra vez
    assert c.editar_empresa("usu_1", "emp_1", {"inscricao_municipal": "12.345-6", "regime_especial": "6"})["inscricao_municipal"] == "123456"


def test_inscricao_municipal_com_mais_de_15_caracteres_ou_simbolo_e_422(c):
    u, e = _conta_com_empresa(c)
    for ruim in ("1" * 16, "12#34"):
        with pytest.raises(contas.Erro) as err:
            c.editar_empresa(u["id"], e["id"], {"inscricao_municipal": ruim})
        assert err.value.status == 422
    assert c.empresa(u["id"], e["id"])["inscricao_municipal"] == ""


def test_regime_especial_fora_do_dominio_do_leiaute_e_422_no_cadastro_e_na_edicao(c):
    u, e = _conta_com_empresa(c)
    with pytest.raises(contas.Erro) as err:
        c.editar_empresa(u["id"], e["id"], {"regime_especial": "7"})
    assert err.value.status == 422
    with pytest.raises(contas.Erro) as err:
        c.cadastrar_empresa(u["id"], "11222333000181", "Outra", 4205407, regime_especial="sociedade")
    assert err.value.status == 422
