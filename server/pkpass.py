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

PERFIL_URL = os.environ.get("PERFIL_URL", "https://hthoni.github.io/backbone/perfil.html")
LOJA_URL = os.environ.get("LOJA_URL", "https://cervejabackbone.com.br/?utm_source=wallet")

BASE = os.path.dirname(__file__)
TEMPLATE_DIR = os.path.join(BASE, "pass_template")   # icon.png, logo.png, primaryLogo.png e @2x/@3x
STRIPS_DIR = os.path.join(BASE, "strips")            # strips/{temporada}/{n}.png
POSTER_DIR = os.path.join(STRIPS_DIR, "estilos", "poster")  # artwork_{estado}.png / @2x / @3x

# ── Poster Generic (iOS 27+) ──────────────────────────────────
# O mesmo pass carrega dois estilos: "storeCard" (cartao de hoje, iOS 26
# e anteriores) e "posterGeneric" (arte de 358x448 pt de ponta a ponta,
# iOS 27+). A Wallet escolhe sozinha o que entende.
#
# As cores de texto sao do pass inteiro (valem para os dois estilos).
# Padrao: branco sobre fundo escuro. Uma arte clara entra aqui como
# excecao, por estado — e precisa ser testada no iPhone antes, porque
# muda tambem o storeCard.
CORES_PADRAO = {
    "foregroundColor": "rgb(255, 255, 255)",
    "backgroundColor": "rgb(0, 0, 0)",
    "labelColor": "rgb(170, 170, 170)",
}
CORES_POR_ESTADO = {
    # "4-10": {"foregroundColor": "rgb(20, 20, 20)", "labelColor": "rgb(60, 60, 60)"},
}

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


def _tipo_recompensa(consumidor):
    """boas_vindas tem prioridade (mesma ordem do /scan); depois punch; senao None."""
    pendentes = [r for r in consumidor.get("recompensas", []) if not r.get("resgatada")]
    if any(r.get("tipo") == "boas_vindas" for r in pendentes):
        return "boas_vindas"
    if pendentes:
        return "punch"
    return None


def _estado(consumidor):
    """
    Nome do estado da cartela, usado para escolher strip, artwork e cores:
    'boasvindas', 'premio' ou '{punches}-{meta}' (ex.: '3-10').
    """
    meta = int(consumidor.get("meta", 10))
    punches = int(consumidor.get("punches", 0))
    tipo = _tipo_recompensa(consumidor)
    if tipo == "boas_vindas":
        return "boasvindas"
    if tipo == "punch" or punches >= meta:
        return "premio"
    return f"{punches}-{meta}"


def _arquivos_strip(temporada, punches, meta, tipo_recompensa):
    """
    Devolve {nome_no_pkpass: bytes} das strips.
    Recompensa de boas-vindas pendente -> strip_estilos_boasvindas.png
    Cartela cheia / recompensa punch    -> strip_estilos_premio.png
    Senao -> strip_estilos_{punches}-{meta}.png  (ex.: strip_estilos_3-10.png)
    Fallback: temporada 'padrao', depois o quadro zero.
    """
    if tipo_recompensa == "boas_vindas":
        nome = "strip_estilos_boasvindas"
    elif tipo_recompensa == "punch" or punches >= meta:
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


def _arquivos_artwork(estado):
    """
    Devolve {artwork.png, artwork@2x.png, artwork@3x.png: bytes} da arte
    do poster para o estado. Os arquivos ja vem nos tamanhos certos
    (358x448, 716x896, 1074x1344). Fallback: estado 0-10. Sem arquivo,
    devolve {} e o pass sai sem o estilo poster (so storeCard).
    """
    meta = estado.split("-")[1] if "-" in estado else "10"
    for nome in (estado, f"0-{meta}", "0-10"):
        base = os.path.join(POSTER_DIR, f"artwork_{nome}")
        if os.path.exists(base + ".png"):
            saida = {}
            for sufixo in ("", "@2x", "@3x"):
                caminho = f"{base}{sufixo}.png"
                if os.path.exists(caminho):
                    with open(caminho, "rb") as f:
                        saida[f"artwork{sufixo}.png"] = f.read()
            return saida
    return {}


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
                f"Apresente este cartão ao(à) atendente a cada chopp Backbone consumido. "
                f"Quando você completar o décimo consumido, você ganhará um Chopp Backbone Pilsen 300 ml.\n\n"
                f"Para resgatar seu chopp, o(a) atendente deve escanear o QR do seu cartão mais uma vez. "
                f"Feito isto, você ganha o chopp e abre uma nova cartela, começando em 0/{meta}.\n\n"
                f"Indique amigos e ganhe pontos com o consumo deles!!\n\n"
                f"Apresente seu QR à câmera do telefone do seu amigo, e ele poderá se cadastrar "
                f"tendo você como indicador!"
            ),
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

    estado = _estado(consumidor)
    cores = dict(CORES_PADRAO)
    cores.update(CORES_POR_ESTADO.get(estado, {}))

    header = [{"key": "indicados", "label": "INDICADOS", "value": str(indicados)}]

    pass_json = {
        "formatVersion": 1,
        "passTypeIdentifier": PASS_TYPE_ID,
        "serialNumber": consumidor["telefone"],
        "teamIdentifier": TEAM_ID,
        "organizationName": ORG_NAME,
        "description": "Clube Backbone",

        # SEM logoText: testado no iPhone, corta.
        # Cores valem para os dois estilos (ver CORES_POR_ESTADO).
        **cores,

        # Sem estes dois campos o pass NUNCA podera receber push.
        "webServiceURL": WEB_SERVICE_URL,
        "authenticationToken": consumidor["auth_token"],

        # ── iOS 26 e anteriores: cartao de hoje ──
        "storeCard": {
            "headerFields": header,
            # primaryFields VAZIO: o texto imprimiria por cima da strip.
            "secondaryFields": campos_frente,
            "backFields": verso,
        },

        # ── iOS 27+: poster de ponta a ponta (artwork_{estado}.png) ──
        # A Apple desenha por cima da arte: primaryLogo + header no topo,
        # QR no centro, primaryFields/footerFields numa faixa escura na base.
        # A arte ja carrega garrafa, nome da cerveja e o X/10.
        "posterGeneric": {
            "headerFields": header,
            "primaryFields": [
                {"key": "socio", "label": "Sócio", "value": consumidor.get("nome", "")},
                {"key": "desde", "label": "Desde", "value": desde},
            ],
            "footerFields": [
                {"key": "progresso", "value": (
                    "Chopp grátis — chame o atendente" if tem_recompensa
                    else f"Chopp {punches} de {meta}"
                )},
            ],
            "backFields": verso,
        },

        # ── iOS 27+: dois botoes abaixo do cartao (iOS antigo ignora) ──
        "featuredActions": [
            {"identifier": "pontos", "type": "membershipBenefits",
             "url": f"{PERFIL_URL}?tel={consumidor['telefone']}"},
            {"identifier": "loja", "type": "shop", "url": LOJA_URL},
        ],

        "barcodes": [{
            "message": f"{WEB_SERVICE_URL}/q/{consumidor['telefone']}",
            "format": "PKBarcodeFormatQR",
            "messageEncoding": "iso-8859-1",
        }],
    }
    return pass_json


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

    # strip da temporada (storeCard, iOS 26 e anteriores)
    meta = int(consumidor.get("meta", 10))
    arquivos.update(_arquivos_strip(
        consumidor.get("temporada", "padrao"),
        int(consumidor.get("punches", 0)),
        meta,
        _tipo_recompensa(consumidor),
    ))

    # artwork do poster (posterGeneric, iOS 27+) — mesmo estado da strip
    artwork = _arquivos_artwork(_estado(consumidor))
    if artwork:
        arquivos.update(artwork)
    else:
        # sem arte, nao oferece o estilo poster (a Wallet cairia num poster vazio)
        pj = json.loads(arquivos["pass.json"])
        pj.pop("posterGeneric", None)
        arquivos["pass.json"] = json.dumps(pj, indent=2, ensure_ascii=False).encode("utf-8")

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
