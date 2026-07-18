# Backbone - Gerador de cartoes Apple Wallet (.pkpass) - v3
#
# Novidades da v3:
#   - webServiceURL + authenticationToken (obrigatorios para push)
#   - strip da temporada, escolhida pelo numero de punches
#   - headerField com o numero de INDICADOS
#   - apelido de torcida como campo central sem rotulo
#   - backField "aviso" com changeMessage %@  -> permite disparar
#     QUALQUER texto de push, inclusive quando nada mais mudou

import os
import json
import hashlib
import zipfile
import io
from cryptography import x509
from cryptography.hazmat.primitives.serialization import load_pem_private_key, pkcs7
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.serialization.pkcs7 import PKCS7Options

import certs

PASS_TYPE_ID = os.environ.get("PASS_TYPE_ID", "pass.com.backbonecervejaria.clube")
TEAM_ID = os.environ.get("APPLE_TEAM_ID", "43BNWL8WH7")
ORG_NAME = os.environ.get("ORG_NAME", "Backbone Cervejaria Artesanal")
WEB_SERVICE_URL = os.environ.get("WEB_SERVICE_URL", "https://backbone-650557630362.us-central1.run.app")
CARDAPIO_URL = os.environ.get("CARDAPIO_URL", "https://hthoni.github.io/backbone/")
WHATSAPP_NUM = os.environ.get("WHATSAPP_NUM", "5521999999999")

BASE = os.path.dirname(__file__)
TEMPLATE_DIR = os.path.join(BASE, "pass_template")   # icon.png, logo.png e @2x/@3x
STRIPS_DIR = os.path.join(BASE, "strips")            # strips/{temporada}/{n}.png

APELIDOS = {
    "Flamengo": "Flamenguista",
    "Botafogo": "Botafoguense",
    "Fluminense": "Fluminense",
    "Vasco": "Vascaíno",
}

_CRED = None


def _credenciais():
    global _CRED
    if _CRED is None:
        _CRED = (
            x509.load_pem_x509_certificate(certs.ler_cert()),
            load_pem_private_key(certs.ler_key(), password=None),
            x509.load_pem_x509_certificate(certs.ler_wwdr()),
        )
    return _CRED


def _arquivos_strip(temporada, punches, meta, tem_recompensa):
    """
    Devolve {nome_no_pkpass: bytes} das strips.
    Cartela cheia OU recompensa pendente -> strip_premio.png
    Senao -> strip_estilos_{punches}-{meta}.png  (ex.: strip_estilos_3-10.png)
    Fallback: temporada 'padrao', depois o quadro zero.
    """
    if tem_recompensa or punches >= meta:
        nome = "strip_estilos_premio"
    else:
        nome = f"strip_estilos_{punches}-{meta}"

    candidatos = [
        os.path.join(STRIPS_DIR, temporada, f"{nome}.png"),
        os.path.join(STRIPS_DIR, "padrao", f"{nome}.png"),
        os.path.join(STRIPS_DIR, temporada, f"strip_estilos_0-{meta}.png"),
    ]
    caminho = next((c for c in candidatos if os.path.exists(c)), None)
    if not caminho:
        return {}

    try:
        from PIL import Image
        base = Image.open(caminho).convert("RGB")
        saida = {}
        for nome_arq, tamanho in (
            ("strip.png", (375, 123)),
            ("strip@2x.png", (750, 246)),
            ("strip@3x.png", (1125, 369)),
        ):
            buf = io.BytesIO()
            base.resize(tamanho, Image.LANCZOS).save(buf, format="PNG")
            saida[nome_arq] = buf.getvalue()
        return saida
    except Exception:
        with open(caminho, "rb") as f:
            dados = f.read()
        return {"strip.png": dados, "strip@2x.png": dados, "strip@3x.png": dados}


def montar_pass_json(consumidor, aviso=None):
    meta = int(consumidor.get("meta", 10))
    punches = int(consumidor.get("punches", 0))
    indicados = len(consumidor.get("indicados", []))
    disponiveis = [r for r in consumidor.get("recompensas", []) if not r.get("resgatada")]
    tem_recompensa = len(disponiveis) > 0

    if tem_recompensa:
        progresso = "Chopp grátis!"
    else:
        progresso = f"{punches} de {meta}"

    apelido = APELIDOS.get(consumidor.get("time", ""), "")

    cadastro = consumidor.get("cadastro_em", "")
    desde = f"{cadastro[5:7]}/{cadastro[2:4]}" if len(cadastro) >= 7 else ""

    campos_frente = [
        {"key": "associado", "label": "ASSOCIADO", "value": consumidor.get("nome", "")},
    ]
    if apelido:
        campos_frente.append({
            "key": "torcida", "value": apelido,
            "textAlignment": "PKTextAlignmentCenter",
        })
    campos_frente.append({
        "key": "desde", "label": "DESDE", "value": desde,
        "textAlignment": "PKTextAlignmentRight",
    })

    # Mensagem no formato que o fluxo do BotConversa espera:
    # "Quero participar do Clube Backbone {palavra}, ... padrinho:{telefone}"
    # A palavra-chave direciona o afilhado ao fluxo do bar do padrinho.
    palavra = consumidor.get("palavra_chave", "") or ""
    _msg = (
        f"Quero participar do Clube Backbone {palavra}, "
        f"a convite do associado padrinho:{consumidor['telefone']}"
    )
    from urllib.parse import quote
    link_indicacao = f"https://wa.me/{WHATSAPP_NUM}?text={quote(_msg)}"

    verso = [
        # ESTE CAMPO E O MOTOR DO PUSH.
        # changeMessage "%@" faz a notificacao repetir o proprio valor.
        # Trocando o valor, disparamos QUALQUER texto - inclusive quando
        # nenhum outro campo mudou (ex.: ponto de indicado descartado).
        {
            "key": "aviso",
            "label": "Último aviso",
            "value": aviso or "Bem-vindo ao Clube Backbone.",
            "changeMessage": "%@",
        },
        {
            "key": "regras",
            "label": "Como funciona",
            "value": (
                f"A cada {meta} chopps consumidos, o próximo é por nossa conta. "
                f"Apresente este cartão ao garçom a cada chopp.\n\n"
                f"Indique amigos: quando um indicado seu fecha a cartela dele, "
                f"você ganha um ponto na sua.\n\n"
                f"Atenção: com a cartela cheia você para de pontuar. "
                f"Resgate seu chopp para abrir uma cartela nova."
            ),
        },
        {
            "key": "indicar",
            "label": "Indique um amigo",
            "attributedValue": f"<a href='{link_indicacao}'>Enviar convite pelo WhatsApp</a>",
        },
        {
            # Texto puro (nao attributedValue): o iOS deixa segurar o dedo e COPIAR.
            # Serve para colar em grupo, Instagram, e-mail - onde o link clicavel nao chega.
            "key": "link_convite",
            "label": "Seu link de convite (toque e segure para copiar)",
            "value": link_indicacao,
        },
        {
            "key": "cardapio",
            "label": "Cardápio",
            "attributedValue": f"<a href='{CARDAPIO_URL}'>Ver os estilos</a>",
        },
        {
            "key": "temporadas",
            "label": "Temporadas completadas",
            "value": ", ".join(consumidor.get("temporadas_completas", [])) or "Nenhuma ainda",
        },
        {
            "key": "aniversario",
            "label": "Aniversário",
            "value": f"{consumidor.get('nascimento_dia','')}/{consumidor.get('nascimento_mes','')}",
        },
    ]

    return {
        "formatVersion": 1,
        "passTypeIdentifier": PASS_TYPE_ID,
        "serialNumber": consumidor["telefone"],
        "teamIdentifier": TEAM_ID,
        "organizationName": ORG_NAME,
        "description": "Clube Backbone",

        # SEM logoText: testado no iPhone, corta.
        "foregroundColor": "rgb(255, 255, 255)",
        "backgroundColor": "rgb(0, 0, 0)",
        "labelColor": "rgb(170, 170, 170)",

        # Sem estes dois campos o pass NUNCA podera receber push.
        "webServiceURL": WEB_SERVICE_URL,
        "authenticationToken": consumidor["auth_token"],

        "storeCard": {
            "headerFields": [
                {"key": "indicados", "label": "INDICADOS", "value": str(indicados)}
            ],
            # primaryFields VAZIO: o texto imprimiria por cima da strip.
            "secondaryFields": campos_frente,
            "backFields": verso,
        },
        "barcodes": [{
            "message": consumidor["telefone"],
            "format": "PKBarcodeFormatQR",
            "messageEncoding": "iso-8859-1",
        }],
    }


def gerar_pkpass(consumidor, aviso=None):
    """Devolve os bytes do .pkpass assinado."""
    cert, key, wwdr = _credenciais()

    arquivos = {}
    arquivos["pass.json"] = json.dumps(
        montar_pass_json(consumidor, aviso), indent=2, ensure_ascii=False
    ).encode("utf-8")

    # icon / logo
    if os.path.isdir(TEMPLATE_DIR):
        for nome in sorted(os.listdir(TEMPLATE_DIR)):
            caminho = os.path.join(TEMPLATE_DIR, nome)
            if os.path.isfile(caminho) and not nome.startswith("."):
                with open(caminho, "rb") as f:
                    arquivos[nome] = f.read()

    # strip da temporada
    meta = int(consumidor.get("meta", 10))
    tem_recompensa = any(
        not r.get("resgatada") for r in consumidor.get("recompensas", [])
    )
    arquivos.update(_arquivos_strip(
        consumidor.get("temporada", "padrao"),
        int(consumidor.get("punches", 0)),
        meta,
        tem_recompensa,
    ))

    # manifest + assinatura
    manifest = {n: hashlib.sha1(d).hexdigest() for n, d in arquivos.items()}
    manifest_bytes = json.dumps(manifest, indent=2).encode("utf-8")
    arquivos["manifest.json"] = manifest_bytes
    arquivos["signature"] = pkcs7.PKCS7SignatureBuilder().set_data(
        manifest_bytes
    ).add_signer(cert, key, hashes.SHA256()).add_certificate(wwdr).sign(
        pkcs7.serialization.Encoding.DER,
        [PKCS7Options.DetachedSignature, PKCS7Options.Binary],
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for nome, dados in arquivos.items():
            zf.writestr(nome, dados)
    return buf.getvalue()
