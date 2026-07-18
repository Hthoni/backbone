# ══════════════════════════════════════════════════════════════
#  CLUBE BACKBONE — GOOGLE WALLET
#
#  Modelo do Google: uma CLASSE (o programa) + um OBJETO por
#  consumidor. O link "Salvar no Google Wallet" e um JWT assinado
#  com a chave da conta de servico. Push = PATCH no objeto.
#
#  Env vars necessarias no Cloud Run:
#    GOOGLE_WALLET_KEY_B64   -> JSON da conta de servico em base64
#    GOOGLE_WALLET_ISSUER_ID -> Issuer ID do Wallet Console
# ══════════════════════════════════════════════════════════════

import os
import json
import base64
import time

from google.oauth2.service_account import Credentials
from google.auth.transport.requests import AuthorizedSession
from google.auth import crypt, jwt as google_jwt

BASE_URL = "https://walletobjects.googleapis.com/walletobjects/v1"
SCOPE = "https://www.googleapis.com/auth/wallet_object.issuer"

ISSUER_ID = os.environ.get("GOOGLE_WALLET_ISSUER_ID", "")
CLASS_SUFFIX = "clube_backbone"

# imagens publicas (GitHub Pages)
LOGO_URI = "https://hthoni.github.io/backbone/clube_logo.png"

_chave_info = None
_sessao = None


def _chave():
    """Carrega o JSON da conta de servico a partir da env var base64."""
    global _chave_info
    if _chave_info is None:
        b64 = os.environ.get("GOOGLE_WALLET_KEY_B64", "")
        if not b64:
            raise RuntimeError("GOOGLE_WALLET_KEY_B64 nao definida")
        _chave_info = json.loads(base64.b64decode(b64))
    return _chave_info


def _http():
    """Sessao HTTP autenticada com a conta de servico."""
    global _sessao
    if _sessao is None:
        creds = Credentials.from_service_account_info(_chave(), scopes=[SCOPE])
        _sessao = AuthorizedSession(creds)
    return _sessao


def id_classe():
    return f"{ISSUER_ID}.{CLASS_SUFFIX}"


def id_objeto(telefone):
    return f"{ISSUER_ID}.{CLASS_SUFFIX}_{telefone}"


# ──────────────────────────────────────────────────────────────
#  CLASSE (o programa de fidelidade — criada uma vez)
# ──────────────────────────────────────────────────────────────

def _payload_classe():
    return {
        "id": id_classe(),
        "issuerName": "Backbone Cervejaria Artesanal",
        "programName": "Clube Backbone",
        "programLogo": {
            "sourceUri": {"uri": LOGO_URI},
            "contentDescription": {
                "defaultValue": {"language": "pt-BR", "value": "Clube Backbone"}
            },
        },
        "reviewStatus": "UNDER_REVIEW",
        "hexBackgroundColor": "#000000",
        "countryCode": "BR",
    }


def garantir_classe():
    """Cria a classe se nao existir. Chamar uma vez (ou sempre — e idempotente)."""
    s = _http()
    r = s.get(f"{BASE_URL}/loyaltyClass/{id_classe()}")
    if r.status_code == 200:
        return {"status": "ja_existe"}
    if r.status_code == 404:
        r2 = s.post(f"{BASE_URL}/loyaltyClass", json=_payload_classe())
        if r2.status_code in (200, 201):
            return {"status": "criada"}
        return {"status": "erro", "detalhe": r2.text}
    return {"status": "erro", "detalhe": r.text}


# ──────────────────────────────────────────────────────────────
#  OBJETO (o cartao de cada consumidor)
# ──────────────────────────────────────────────────────────────

def _payload_objeto(consumidor):
    tel = consumidor["telefone"]
    punches = int(consumidor.get("punches", 0))
    meta = int(consumidor.get("meta", 10))

    pendentes = [r for r in consumidor.get("recompensas", []) if not r.get("resgatada")]
    tem_boas_vindas = any(r.get("tipo") == "boas_vindas" for r in pendentes)
    tem_premio = any(r.get("tipo") != "boas_vindas" for r in pendentes)

    if tem_boas_vindas:
        situacao = "Chopp de boas-vindas disponível — chame o atendente!"
    elif tem_premio or punches >= meta:
        situacao = "CHOPP GRÁTIS liberado — chame o atendente!"
    else:
        situacao = f"Faltam {meta - punches} chopps para o próximo grátis."

    aviso = consumidor.get("aviso", "")

    return {
        "id": id_objeto(tel),
        "classId": id_classe(),
        "state": "ACTIVE",
        "accountId": tel,
        "accountName": consumidor.get("nome", ""),
        "loyaltyPoints": {
            "label": "Chopps",
            "balance": {"string": f"{punches} de {meta}"},
        },
        "secondaryLoyaltyPoints": {
            "label": "Indicados",
            "balance": {"string": str(len(consumidor.get("indicados", [])))},
        },
        "barcode": {
            "type": "QR_CODE",
            "value": tel,
            "alternateText": tel,
        },
        "textModulesData": [
            {"id": "situacao", "header": "Situação", "body": situacao},
            {"id": "aviso", "header": "Último aviso", "body": aviso or "—"},
            {
                "id": "regras",
                "header": "Como funciona",
                "body": f"A cada {meta} chopps, o próximo é por nossa conta. "
                        "Apresente este cartão ao atendente a cada pedido.",
            },
        ],
    }


def atualizar_objeto(consumidor):
    """
    Cria ou atualiza o objeto do consumidor no Google.
    E o equivalente do push da Apple: quem tem o cartao salvo
    ve a mudanca automaticamente.
    """
    s = _http()
    corpo = _payload_objeto(consumidor)
    oid = corpo["id"]
    r = s.get(f"{BASE_URL}/loyaltyObject/{oid}")
    if r.status_code == 200:
        r2 = s.put(f"{BASE_URL}/loyaltyObject/{oid}", json=corpo)
    elif r.status_code == 404:
        r2 = s.post(f"{BASE_URL}/loyaltyObject", json=corpo)
    else:
        return {"status": "erro", "detalhe": r.text}
    if r2.status_code in (200, 201):
        return {"status": "ok"}
    return {"status": "erro", "detalhe": r2.text}


# ──────────────────────────────────────────────────────────────
#  LINK "SALVAR NO GOOGLE WALLET" (JWT assinado)
# ──────────────────────────────────────────────────────────────

def link_salvar(consumidor):
    """
    Gera o link https://pay.google.com/gp/v/save/{jwt}.
    O objeto vai DENTRO do JWT: e criado quando a pessoa salva.
    """
    chave = _chave()
    claims = {
        "iss": chave["client_email"],
        "aud": "google",
        "typ": "savetowallet",
        "iat": int(time.time()),
        "payload": {
            "loyaltyClasses": [_payload_classe()],
            "loyaltyObjects": [_payload_objeto(consumidor)],
        },
        "origins": ["https://hthoni.github.io"],
    }
    signer = crypt.RSASigner.from_service_account_info(chave)
    token = google_jwt.encode(signer, claims).decode("utf-8")
    return f"https://pay.google.com/gp/v/save/{token}"
