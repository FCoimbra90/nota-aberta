"""Login com Google (OAuth 2.0, código com PKCE). Copiado de apps/sistemas/agenda-clinica/oauth.py (cada sistema é
uma imagem própria); só o Google fica, e o erro vira contas.Erro.

Sem biblioteca: só urllib.

Só vale o e-mail VERIFICADO pelo provedor: conta com e-mail não verificado é recusada (senão qualquer um criaria conta com o e-mail de outra pessoa).
O botão de cada provedor só aparece se o servidor tem as credenciais dele (GOOGLE_CLIENT_ID/_SECRET, GITHUB_CLIENT_ID/_SECRET)."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request

from contas import Erro

# Endereços dos provedores (os testes apontam para um provedor falso local).
PROVEDORES = {
    "google": {"rotulo": "Google", "auth": "https://accounts.google.com/o/oauth2/v2/auth", "token": "https://oauth2.googleapis.com/token",
               "usuario": "https://openidconnect.googleapis.com/v1/userinfo", "escopo": "openid email profile"},
}


def _credenciais(prov: str) -> tuple[str, str]:
    p = prov.upper()
    return os.environ.get(f"{p}_CLIENT_ID", ""), os.environ.get(f"{p}_CLIENT_SECRET", "")


def configurado(prov: str) -> bool:
    return prov in PROVEDORES and all(_credenciais(prov))


def disponiveis() -> list[str]:
    return [p for p in PROVEDORES if configurado(p)]


def retorno(base: str, prov: str) -> str:
    return f"{base}/entrar/{prov}/retorno"


def _desafio(verificador: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verificador.encode()).digest()).rstrip(b"=").decode()


def iniciar(prov: str, base: str) -> tuple[str, str]:
    """(endereço do provedor para onde redirecionar, valor do cookie `state.verificador`)."""
    estado, verif = secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    q = {"client_id": _credenciais(prov)[0], "redirect_uri": retorno(base, prov), "response_type": "code", "scope": PROVEDORES[prov]["escopo"],
         "state": estado, "code_challenge": _desafio(verif), "code_challenge_method": "S256"}
    return PROVEDORES[prov]["auth"] + "?" + urllib.parse.urlencode(q), f"{estado}.{verif}"


def _http(req: urllib.request.Request) -> dict | list:
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        if 400 <= e.code < 500:  # o provedor recusou o código, o segredo ou o PKCE: é falha de quem pediu, não do provedor
            raise Erro(401, "codigo_invalido", "o provedor recusou o código de login; tente de novo") from None
        raise Erro(502, "provedor_indisponivel", "o provedor de login falhou; tente de novo ") from None
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        raise Erro(502, "provedor_indisponivel", "não foi possível falar com o provedor de login; tente de novo ") from None


def identidade(prov: str, codigo: str, base: str, verificador: str) -> dict:
    """Troca o código por um token e lê quem é: {sub, email, verificado, nome}."""
    cfg, (cid, segredo) = PROVEDORES[prov], _credenciais(prov)
    corpo = urllib.parse.urlencode({"client_id": cid, "client_secret": segredo, "code": codigo, "redirect_uri": retorno(base, prov),
                                    "grant_type": "authorization_code", "code_verifier": verificador}).encode()
    tk = _http(urllib.request.Request(cfg["token"], data=corpo, headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"}))
    token = tk.get("access_token") if isinstance(tk, dict) else None
    if not token:
        raise Erro(401, "codigo_invalido", "o provedor recusou o código de login; tente de novo")
    def pega(url: str):
        return _http(urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json", "User-Agent": "nota-aberta"}))

    u = pega(cfg["usuario"])
    return {"sub": str(u.get("sub", "")), "email": str(u.get("email", "")).lower(), "verificado": u.get("email_verified") is True,
            "nome": str(u.get("name", ""))}
