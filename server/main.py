# Backbone Beer - Clube Backbone - v3
#
# Mudancas em relacao a v2:
#   - /scan unificado: o garcom faz UM gesto. Se ha recompensa, resgata.
#     Se nao ha, pontua. A cartela SO zera no resgate.
#   - meta e temporada congeladas por ciclo
#   - member gets member: padrinho / indicados
#   - web service da Apple (4 endpoints) + push via APNs
#   - /cartao/<telefone>: entrega o .pkpass

import os
import json
import uuid
import secrets
import re
from datetime import datetime, timezone
from flask import Flask, request, jsonify, Response
from flask_cors import CORS

import storage
import pkpass
import apns
import gwallet

app = Flask(__name__)
CORS(app)

PASS_TYPE_ID = os.environ.get("PASS_TYPE_ID", "pass.com.backbonecervejaria.clube")


def agora():
    return datetime.now(timezone.utc).isoformat()


def so_digitos(s):
    return "".join(c for c in (s or "") if c.isdigit())


def _disponiveis(c):
    return [r for r in c.get("recompensas", []) if not r.get("resgatada")]


def _garcom_pertence_bar(garcom, bar):
    return garcom.get("bar_id") == bar


def _notificar(consumidor, aviso):
    """Grava o aviso no consumidor e dispara o push (Apple + Google)."""
    consumidor["aviso"] = aviso
    consumidor["atualizado_em"] = agora()
    storage.salvar_consumidor(consumidor)

    # Google Wallet: atualizar o objeto e o "push" do Android
    try:
        gwallet.atualizar_objeto(consumidor)
    except Exception as e:
        app.logger.warning("gwallet falhou: %s", e)

    try:
        return apns.enviar_push(consumidor["telefone"])
    except Exception as e:
        app.logger.warning("push falhou: %s", e)
        return {"enviados": 0, "erro": str(e)}


# ══════════════════════════════════════════════════════════════
#  CADASTRO
# ══════════════════════════════════════════════════════════════

@app.route("/cadastro/<bar>/<telefone>/<nome>/<dia>/<mes>/<time>")
def cadastro(bar, telefone, nome, dia, mes, time):
    from urllib.parse import unquote
    nome = unquote(nome)
    bar = unquote(bar)
    time = unquote(time)
    telefone = so_digitos(telefone)

    # indicador vem por query string: ?ind=5521999887766
    indicador_tel = so_digitos(request.args.get("padrinho", ""))
    palavra_chave = (request.args.get("palavra", "") or "").strip()
    cep = so_digitos(request.args.get("cep", ""))[:8]
    if indicador_tel == telefone:
        indicador_tel = ""  # auto-indicacao bloqueada

    # TRAVA ANTIFRAUDE: lista fria POR BAR (numeros vetados pelo dono)
    for b in storage.carregar_bloqueados():
        if b["telefone"] == telefone and (b["bar"] == "*" or b["bar"] == bar):
            return jsonify({"status": "nao_permitido",
                            "motivo": "Cadastro não disponível para este telefone."}), 403

    # TRAVA ANTIFRAUDE: funcionario (garcom) nao pode ser associado
    for _g in storage.listar_garcons():
        if _g.get("telefone") and so_digitos(_g["telefone"]) == telefone:
            return jsonify({"status": "nao_permitido",
                            "motivo": "Telefone vinculado a um funcionário."}), 403

    existente = storage.carregar_consumidor(telefone)
    if existente:
        return jsonify({"status": "ja_cadastrado", "consumidor": existente})

    config = storage.carregar_config()

    indicador = storage.carregar_consumidor(indicador_tel) if indicador_tel else None

    consumidor = {
        "telefone": telefone,
        "nome": nome,
        "indicador": bar,
        "palavra_chave": palavra_chave,
        "cep": cep,
        "nascimento_dia": dia,
        "nascimento_mes": mes,
        "time": time,

        "punches": 0,
        "meta": int(config["punches_para_recompensa"]),        # CONGELADA
        "temporada": config["temporada_vigente"],              # CONGELADA

        "padrinho": indicador["telefone"] if indicador else None,
        "indicados": [],

        "recompensas": [
            {"tipo": "boas_vindas", "gerada_em": agora(), "resgatada": False}
        ],
        "recompensas_total": 0,
        "ciclos_completos": 0,
        "temporadas_completas": [],

        "auth_token": secrets.token_hex(20),   # unico por consumidor
        "aviso": "Bem-vindo ao Clube Backbone! Seu primeiro chopp é por nossa conta.",
        "ultimo_punch": None,
        "cadastro_em": agora(),
        "atualizado_em": agora(),
        "ativo": True,
    }
    storage.salvar_consumidor(consumidor)

    if indicador:
        indicador.setdefault("indicados", [])
        if telefone not in indicador["indicados"]:
            indicador["indicados"].append(telefone)
        _notificar(indicador, f"{nome} entrou no Clube pelo seu convite. Quando fechar a cartela, você ganha um ponto.")

    storage.salvar_evento({
        "id": str(uuid.uuid4()), "tipo": "cadastro", "telefone": telefone,
        "bar": bar, "garcom_id": None, "padrinho": consumidor["padrinho"],
        "data": agora(),
    })

    return jsonify({"status": "cadastrado", "consumidor": consumidor})


@app.route("/consumidor/<telefone>")
def consultar_consumidor(telefone):
    c = storage.carregar_consumidor(so_digitos(telefone))
    if not c:
        return jsonify({"erro": "nao_encontrado"}), 404
    c["punches_para_recompensa"] = c.get("meta", 10)
    c["recompensas_disponiveis"] = len(_disponiveis(c))
    c["total_indicados"] = len(c.get("indicados", []))
    return jsonify(c)


# ══════════════════════════════════════════════════════════════
#  SCAN - o gesto unico do garcom
# ══════════════════════════════════════════════════════════════

def _creditar_indicador(consumidor):
    """Chamado quando o consumidor FECHA a cartela. Credita 1 ponto ao indicador."""
    tel_ind = consumidor.get("padrinho")
    if not tel_ind:
        return

    ind = storage.carregar_consumidor(tel_ind)
    if not ind or not ind.get("ativo", True):
        return

    # cartela cheia -> ponto DESCARTADO
    if _disponiveis(ind):
        _notificar(ind, "Um indicado seu fechou a meta, mas sua cartela está cheia. "
                        "Venha resgatar seu chopp para voltar a pontuar.")
        storage.salvar_evento({
            "id": str(uuid.uuid4()), "tipo": "ponto_indicacao_descartado",
            "telefone": tel_ind, "origem": consumidor["telefone"], "data": agora(),
        })
        return

    meta_ind = int(ind.get("meta", 10))
    ind["punches"] = int(ind.get("punches", 0)) + 1

    if ind["punches"] >= meta_ind:
        ind["recompensas"].append({"tipo": "punch", "gerada_em": agora(), "resgatada": False})
        ind["recompensas_total"] = ind.get("recompensas_total", 0) + 1
        aviso = ("Um indicado seu fechou a meta — e fechou a sua cartela! "
                 "Chame o atendente e resgate seu chopp.")
    else:
        faltam = meta_ind - ind["punches"]
        aviso = f"Parabéns! Seu indicado fechou a meta e você ganhou um ponto. Faltam {faltam} para o seu chopp."

    _notificar(ind, aviso)
    storage.salvar_evento({
        "id": str(uuid.uuid4()), "tipo": "ponto_indicacao",
        "telefone": tel_ind, "origem": consumidor["telefone"], "data": agora(),
    })


@app.route("/scan", methods=["POST"])
def scan():
    dados = request.get_json() or {}
    telefone = so_digitos(dados.get("telefone"))
    garcom_id = dados.get("garcom_id")
    bar = dados.get("bar")

    if not telefone or not garcom_id or not bar:
        return jsonify({"erro": "dados_incompletos"}), 400

    consumidor = storage.carregar_consumidor(telefone)
    if not consumidor:
        return jsonify({"erro": "consumidor_nao_encontrado"}), 404

    # quem esta escaneando pode ser um garcom, um gestor, OU um admin
    # (admin e gestor entram no scanner com a propria credencial, sem
    # precisar de um cadastro de garcom a parte)
    garcom = storage.carregar_garcom(garcom_id)
    if garcom:
        if not _garcom_pertence_bar(garcom, bar):
            return jsonify({"erro": "bar_diferente"}), 403
    else:
        gestor = storage.carregar_gestor(garcom_id)
        if gestor:
            if "*" not in gestor.get("bar_ids", []) and bar not in gestor.get("bar_ids", []):
                return jsonify({"erro": "bar_diferente"}), 403
        else:
            admin = storage.carregar_admin(garcom_id)
            if not admin:
                return jsonify({"erro": "garcom_nao_encontrado"}), 404
            # admin pode escanear em qualquer bar, sem checagem adicional

    config = storage.carregar_config()
    consumidor.setdefault("recompensas", [])
    consumidor.setdefault("meta", int(config["punches_para_recompensa"]))
    consumidor.setdefault("temporada", config["temporada_vigente"])

    pendentes = _disponiveis(consumidor)

    # ─────────── CAMINHO 1: tem recompensa -> RESGATA ───────────
    if pendentes:
        pendentes.sort(key=lambda r: 0 if r.get("tipo") == "boas_vindas" else 1)
        recompensa = pendentes[0]

        for r in consumidor["recompensas"]:
            if r is recompensa:
                r["resgatada"] = True
                r["resgatada_em"] = agora()
                r["resgatada_bar"] = bar
                r["resgatada_garcom"] = garcom_id
                break

        tipo = recompensa.get("tipo")

        if tipo == "punch":
            # fecha o ciclo: nova cartela, nova meta, nova temporada
            temporada_antiga = consumidor.get("temporada")
            consumidor["punches"] = 0
            consumidor["meta"] = int(config["punches_para_recompensa"])
            consumidor["temporada"] = config["temporada_vigente"]
            consumidor["ciclos_completos"] = consumidor.get("ciclos_completos", 0) + 1
            consumidor.setdefault("temporadas_completas", [])
            if temporada_antiga and temporada_antiga not in consumidor["temporadas_completas"]:
                consumidor["temporadas_completas"].append(temporada_antiga)
            aviso = f"Chopp resgatado! Nova cartela aberta — temporada {consumidor['temporada']}."
        else:
            # boas-vindas: NAO mexe na cartela nem gera punch
            aviso = "Chopp de boas-vindas resgatado! Agora é só juntar pontos."

        push = _notificar(consumidor, aviso)
        storage.salvar_evento({
            "id": str(uuid.uuid4()), "tipo": "resgate", "tipo_recompensa": tipo,
            "telefone": telefone, "bar": bar, "garcom_id": garcom_id, "data": agora(),
        })

        return jsonify({
            "status": "ok", "acao": "resgate", "tipo_resgatado": tipo,
            "nome": consumidor["nome"],
            "punches": consumidor["punches"], "meta": consumidor["meta"],
            "recompensas_disponiveis": len(_disponiveis(consumidor)),
            "mensagem_garcom": f"RESGATE — entregue 1 chopp grátis para {consumidor['nome']}.",
            "push": push,
        })

    # ─────────── CAMINHO 2: sem recompensa -> PONTUA ───────────
    meta = int(consumidor["meta"])
    consumidor["punches"] = int(consumidor.get("punches", 0)) + 1
    consumidor["ultimo_punch"] = agora()

    fechou = consumidor["punches"] >= meta

    if fechou:
        # a cartela NAO zera aqui. Ela trava cheia ate o resgate.
        consumidor["punches"] = meta
        consumidor["recompensas"].append({"tipo": "punch", "gerada_em": agora(), "resgatada": False})
        consumidor["recompensas_total"] = consumidor.get("recompensas_total", 0) + 1
        aviso = ("Cartela fechada! Seu próximo chopp é por nossa conta. "
                 "Chame o atendente para resgatar.")
    else:
        faltam = meta - consumidor["punches"]
        aviso = f"Chopp registrado! Faltam {faltam} para o seu chopp grátis."

    push = _notificar(consumidor, aviso)

    if fechou:
        _creditar_indicador(consumidor)

    storage.salvar_evento({
        "id": str(uuid.uuid4()), "tipo": "punch", "telefone": telefone, "bar": bar,
        "garcom_id": garcom_id, "recompensa_gerada": fechou, "data": agora(),
    })

    return jsonify({
        "status": "ok", "acao": "punch",
        "nome": consumidor["nome"],
        "punches": consumidor["punches"], "meta": meta,
        "recompensa_gerada": fechou,
        "recompensas_disponiveis": len(_disponiveis(consumidor)),
        "mensagem_garcom": (
            f"CARTELA FECHADA — {consumidor['nome']} tem 1 chopp grátis. Avise o cliente."
            if fechou else
            f"Ponto registrado — {consumidor['nome']}: {consumidor['punches']} de {meta}."
        ),
        "push": push,
    })


# rotas antigas continuam funcionando (o scanner atual ainda as usa)
@app.route("/punch", methods=["POST"])
def punch_legado():
    return scan()


@app.route("/resgatar", methods=["POST"])
def resgatar_legado():
    return scan()


# ══════════════════════════════════════════════════════════════
#  CARTAO .pkpass
# ══════════════════════════════════════════════════════════════

@app.route("/cartao/<telefone>")
def cartao(telefone):
    telefone = so_digitos(telefone)
    c = storage.carregar_consumidor(telefone)
    if not c:
        return jsonify({"erro": "consumidor_nao_encontrado"}), 404

    if not c.get("auth_token"):
        c["auth_token"] = secrets.token_hex(20)
        storage.salvar_consumidor(c)

    try:
        dados = pkpass.gerar_pkpass(c, aviso=c.get("aviso"))
    except Exception as e:
        app.logger.exception("falha ao gerar pkpass")
        return jsonify({"erro": "falha_ao_gerar_cartao", "detalhe": str(e)}), 500

    return Response(dados, mimetype="application/vnd.apple.pkpass", headers={
        "Content-Disposition": f'attachment; filename="clube-backbone-{telefone}.pkpass"',
        "Cache-Control": "no-store",
    })


# ══════════════════════════════════════════════════════════════
#  WEB SERVICE DA APPLE — as 4 rotas exigidas
#  Os caminhos e nomes sao definidos pela Apple. Nao mudar.
# ══════════════════════════════════════════════════════════════

def _autorizado(consumidor):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("ApplePass "):
        return False
    return auth[10:].strip() == consumidor.get("auth_token")


@app.route("/v1/devices/<device_id>/registrations/<pass_type_id>/<serial>", methods=["POST"])
def registrar_dispositivo(device_id, pass_type_id, serial):
    """O iPhone chama isto ao instalar o cartao, entregando o pushToken."""
    c = storage.carregar_consumidor(so_digitos(serial))
    if not c:
        return "", 404
    if not _autorizado(c):
        return "", 401

    push_token = (request.get_json(silent=True) or {}).get("pushToken")
    if not push_token:
        return "", 400

    registro = storage.carregar_registro(serial) or {}
    ja_existia = device_id in registro.get("dispositivos", {})

    storage.salvar_registro(serial, device_id, push_token)
    # 201 = registro novo | 200 = ja estava registrado
    return "", (200 if ja_existia else 201)


@app.route("/v1/devices/<device_id>/registrations/<pass_type_id>", methods=["GET"])
def listar_passes_atualizados(device_id, pass_type_id):
    """O iPhone pergunta: o que mudou desde X?"""
    desde = request.args.get("passesUpdatedSince")
    seriais = storage.seriais_do_dispositivo(device_id)
    if not seriais:
        return "", 204

    alterados, ultimo = [], desde or ""
    for s in seriais:
        c = storage.carregar_consumidor(s)
        if not c:
            continue
        atualizado = c.get("atualizado_em", "")
        if not desde or atualizado > desde:
            alterados.append(s)
        if atualizado > ultimo:
            ultimo = atualizado

    if not alterados:
        return "", 204

    return jsonify({"serialNumbers": alterados, "lastUpdated": ultimo})


@app.route("/v1/passes/<pass_type_id>/<serial>", methods=["GET"])
def baixar_pass_atualizado(pass_type_id, serial):
    """O iPhone acordou com o push e vem buscar o .pkpass novo."""
    c = storage.carregar_consumidor(so_digitos(serial))
    if not c:
        return "", 404
    if not _autorizado(c):
        return "", 401

    dados = pkpass.gerar_pkpass(c, aviso=c.get("aviso"))
    return Response(dados, mimetype="application/vnd.apple.pkpass", headers={
        "Last-Modified": c.get("atualizado_em", ""),
    })


@app.route("/v1/devices/<device_id>/registrations/<pass_type_id>/<serial>", methods=["DELETE"])
def remover_dispositivo(device_id, pass_type_id, serial):
    """O usuario apagou o cartao da Wallet."""
    c = storage.carregar_consumidor(so_digitos(serial))
    if not c:
        return "", 404
    if not _autorizado(c):
        return "", 401
    storage.remover_registro(device_id, serial)
    return "", 200


@app.route("/v1/log", methods=["POST"])
def log_apple():
    """A Apple manda os erros dela para ca. Otimo para depurar."""
    app.logger.warning("APPLE LOG: %s", json.dumps(request.get_json(silent=True) or {}))
    return "", 200


# ══════════════════════════════════════════════════════════════
#  ADMIN
# ══════════════════════════════════════════════════════════════

@app.route("/login", methods=["POST"])
def login():
    dados = request.get_json() or {}
    garcom = storage.carregar_garcom(dados.get("id"))
    if not garcom or garcom.get("senha") != dados.get("senha"):
        return jsonify({"erro": "credenciais_invalidas"}), 401
    if not garcom.get("ativo", True):
        return jsonify({"erro": "garcom_inativo"}), 403
    return jsonify({
        "status": "ok", "garcom_id": garcom["id"], "nome": garcom["nome"],
        "bar_id": garcom["bar_id"], "bar_nome": garcom.get("bar_nome", ""),
        "tutorial_visto": garcom.get("tutorial_visto", False),
    })


@app.route("/garcom/<garcom_id>/tutorial-visto", methods=["POST"])
def marcar_tutorial_visto(garcom_id):
    """
    Marca (ou desmarca, via {"visto": false} no corpo) se o garcom ja viu
    o tutorial de 'Como funciona'. O reset e usado pelo gestor quando o
    garcom troca de aparelho ou precisa rever o tutorial.
    So mexe nesse campo — preserva todo o resto do cadastro
    (senha, telefone, bar, etc.), ao contrario de reusar o POST de
    criacao/edicao que sobrescreveria o registro inteiro.
    """
    garcom = storage.carregar_garcom(garcom_id)
    if not garcom:
        return jsonify({"erro": "nao_encontrado"}), 404
    dados = request.get_json(silent=True) or {}
    garcom["tutorial_visto"] = bool(dados.get("visto", True))
    storage.salvar_garcom(garcom)
    return jsonify({"status": "ok"})


@app.route("/admin/bares")
def admin_bares():
    return jsonify(storage.listar_bares())


@app.route("/admin/bares", methods=["POST"])
def admin_criar_bar():
    dados = request.get_json()
    dados.setdefault("id", str(uuid.uuid4())[:8])
    storage.salvar_bar(dados)
    return jsonify({"status": "ok", "bar": dados})


@app.route("/admin/garcons")
def admin_garcons():
    return jsonify(storage.listar_garcons())


@app.route("/admin/garcons", methods=["POST"])
def admin_criar_garcom():
    dados = request.get_json()
    tel_g = so_digitos(dados.get("telefone", ""))
    if tel_g and storage.carregar_consumidor(tel_g):
        return jsonify({"erro": "telefone_de_associado",
                        "detalhe": "Este telefone pertence a um associado. "
                                   "Funcionário não pode ser associado — apague o cadastro dele antes."}), 409
    dados["telefone"] = tel_g

    existente = storage.carregar_garcom(dados.get("id")) if dados.get("id") else None
    if existente and not dados.get("senha"):
        dados["senha"] = existente.get("senha")  # edicao sem trocar senha: mantem a atual

    dados.setdefault("id", str(uuid.uuid4())[:8])
    storage.salvar_garcom(dados)
    return jsonify({"status": "ok", "garcom": dados})


@app.route("/admin/consumidores")
def admin_consumidores():
    bar_filtro = request.args.get("bar")
    todos = storage.listar_consumidores()
    if bar_filtro:
        todos = [c for c in todos if c.get("indicador") == bar_filtro]
    for c in todos:
        c["recompensas_disponiveis"] = len(_disponiveis(c))
        c["total_indicados"] = len(c.get("indicados", []))
        c.pop("auth_token", None)   # nunca expor
    return jsonify(todos)


@app.route("/admin/config")
def admin_config():
    return jsonify(storage.carregar_config())


@app.route("/admin/config", methods=["POST"])
def admin_salvar_config():
    storage.salvar_config(request.get_json())
    return jsonify({"status": "ok"})


@app.route("/admin/push/<telefone>", methods=["POST"])
def admin_push_manual(telefone):
    """Dispara um push avulso. Base para campanhas segmentadas."""
    c = storage.carregar_consumidor(so_digitos(telefone))
    if not c:
        return jsonify({"erro": "nao_encontrado"}), 404
    texto = (request.get_json() or {}).get("texto", "")
    if not texto:
        return jsonify({"erro": "texto_vazio"}), 400
    return jsonify(_notificar(c, texto))


@app.route("/admin/consumidores/<telefone>", methods=["DELETE"])
def admin_apagar_consumidor(telefone):
    telefone = so_digitos(telefone)
    c = storage.carregar_consumidor(telefone)
    if not c:
        return jsonify({"erro": "nao_encontrado"}), 404
    storage.apagar_consumidor(telefone)
    return jsonify({"status": "apagado", "telefone": telefone})


@app.route("/admin/bares/<bar_id>", methods=["DELETE"])
def admin_apagar_bar(bar_id):
    b = storage.carregar_bar(bar_id)
    if not b:
        return jsonify({"erro": "nao_encontrado"}), 404
    storage.apagar_bar(bar_id)
    return jsonify({"status": "apagado", "id": bar_id})


@app.route("/admin/garcons/<garcom_id>", methods=["DELETE"])
def admin_apagar_garcom(garcom_id):
    g = storage.carregar_garcom(garcom_id)
    if not g:
        return jsonify({"erro": "nao_encontrado"}), 404
    storage.apagar_garcom(garcom_id)
    return jsonify({"status": "apagado", "id": garcom_id})


# ══════════════════════════════════════════════════════════════
#  GESTORES — login e cadastro do dono/gerente do bar
#  (painel Historico / Atual / Atividade / Garcons)
# ══════════════════════════════════════════════════════════════

@app.route("/login-gestor", methods=["POST"])
def login_gestor():
    """
    Login do painel do gestor. Separado do /login do garcom porque o
    papel e os dados devolvidos sao diferentes (bar_ids em vez de um bar so).
    """
    dados = request.get_json() or {}
    gestor = storage.carregar_gestor(dados.get("id"))
    if not gestor or gestor.get("senha") != dados.get("senha"):
        return jsonify({"erro": "credenciais_invalidas"}), 401
    if not gestor.get("ativo", True):
        return jsonify({"erro": "gestor_inativo"}), 403

    bar_ids = gestor.get("bar_ids", [])
    bares_map = {b["id"]: b.get("nome", b["id"]) for b in storage.listar_bares()}
    bares_map["*"] = "Todos os bares"

    return jsonify({
        "status": "ok",
        "gestor_id": gestor["id"],
        "nome": gestor["nome"],
        # se so tiver 1 bar, o front pode pular a tela de selecao e ir direto
        "bares": [{"id": bid, "nome": bares_map.get(bid, bid)} for bid in bar_ids],
    })


@app.route("/admin/gestores")
def admin_gestores():
    gestores = storage.listar_gestores()
    for g in gestores:
        g.pop("senha", None)   # nunca expor
    return jsonify(gestores)


@app.route("/admin/gestores", methods=["POST"])
def admin_criar_gestor():
    """
    Body: {"nome":..., "id":..., "senha":..., "telefone":..., "bar_ids": [...]}
    Sem trava de associado aqui (diferente do garcom): um associado
    PODE ser gestor — sao papeis diferentes, sem conflito de fluxo.
    """
    dados = request.get_json()
    dados["telefone"] = so_digitos(dados.get("telefone", ""))

    existente = storage.carregar_gestor(dados.get("id")) if dados.get("id") else None
    if existente and not dados.get("senha"):
        dados["senha"] = existente.get("senha")  # edicao sem trocar senha: mantem a atual

    dados.setdefault("id", str(uuid.uuid4())[:8])
    dados.setdefault("bar_ids", [])
    dados.setdefault("ativo", True)
    storage.salvar_gestor(dados)

    resposta = dict(dados)
    resposta.pop("senha", None)
    return jsonify({"status": "ok", "gestor": resposta})


@app.route("/admin/gestores/<gestor_id>", methods=["DELETE"])
def admin_apagar_gestor(gestor_id):
    g = storage.carregar_gestor(gestor_id)
    if not g:
        return jsonify({"erro": "nao_encontrado"}), 404
    storage.apagar_gestor(gestor_id)
    return jsonify({"status": "apagado", "id": gestor_id})


# ══════════════════════════════════════════════════════════════
#  ADMINS — acesso total (login.html tenta garcom, depois gestor,
#  depois admin). Um admin acessa o admin.html E o gestor.html
#  (como gestor master, ve "*" = todos os bares).
# ══════════════════════════════════════════════════════════════

@app.route("/login-admin", methods=["POST"])
def login_admin():
    dados = request.get_json() or {}
    admin = storage.carregar_admin(dados.get("id"))
    if not admin or admin.get("senha") != dados.get("senha"):
        return jsonify({"erro": "credenciais_invalidas"}), 401
    if not admin.get("ativo", True):
        return jsonify({"erro": "admin_inativo"}), 403
    return jsonify({"status": "ok", "admin_id": admin["id"], "nome": admin["nome"]})


@app.route("/admin/admins")
def admin_listar_admins():
    admins = storage.listar_admins()
    for a in admins:
        a.pop("senha", None)
    return jsonify(admins)


@app.route("/admin/admins", methods=["POST"])
def admin_criar_admin():
    dados = request.get_json()
    existente = storage.carregar_admin(dados.get("id")) if dados.get("id") else None
    if existente and not dados.get("senha"):
        dados["senha"] = existente.get("senha")
    dados.setdefault("id", str(uuid.uuid4())[:8])
    dados.setdefault("ativo", True)
    storage.salvar_admin(dados)
    resposta = dict(dados)
    resposta.pop("senha", None)
    return jsonify({"status": "ok", "admin": resposta})


@app.route("/admin/admins/<admin_id>", methods=["DELETE"])
def admin_apagar_admin(admin_id):
    a = storage.carregar_admin(admin_id)
    if not a:
        return jsonify({"erro": "nao_encontrado"}), 404
    storage.apagar_admin(admin_id)
    return jsonify({"status": "apagado", "id": admin_id})


@app.route("/gwallet/<telefone>")
def gwallet_link(telefone):
    """Redireciona para o link 'Salvar no Google Wallet' do consumidor."""
    c = storage.carregar_consumidor(so_digitos(telefone))
    if not c:
        return jsonify({"erro": "nao_encontrado"}), 404
    try:
        url = gwallet.link_salvar(c)
        return Response(status=302, headers={"Location": url})
    except Exception as e:
        return jsonify({"erro": "gwallet_indisponivel", "detalhe": str(e)}), 500


@app.route("/admin/gwallet/classe", methods=["POST"])
def gwallet_criar_classe():
    """Cria a classe do programa no Google (rodar uma vez)."""
    try:
        return jsonify(gwallet.garantir_classe())
    except Exception as e:
        return jsonify({"erro": str(e)}), 500


@app.route("/admin/migrar-eventos", methods=["POST"])
def admin_migrar_eventos():
    """
    RODAR UMA VEZ, logo apos subir esta versao.
    Junta os eventos antigos (1 arquivo por evento) num log unico
    (eventos/log.jsonl) — e o que deixa o painel do gestor rapido.
    Nao apaga nenhum arquivo antigo. Seguro chamar de novo (nao duplica).
    """
    try:
        return jsonify(storage.migrar_eventos_para_log())
    except Exception as e:
        return jsonify({"erro": str(e)}), 500


@app.route("/admin/garcons/stats")
def garcons_stats():
    """
    Estatisticas de scans por garcom.
    ?dias=30 define a janela recente (default 30).
    Categorias:
      cadastros -> resgates de boas_vindas (cliente novo ativado)
      consumos  -> punches (chopps pagos)
      resgates  -> resgates de premio (meta atingida)
    """
    from datetime import timedelta
    dias = int(request.args.get("dias", 30))
    corte = (datetime.now(timezone.utc) - timedelta(days=dias)).isoformat()

    stats = {}
    for ev in storage.listar_eventos():
        g = ev.get("garcom_id")
        if not g:
            continue
        tipo = ev.get("tipo")
        if tipo == "punch":
            cat = "consumos"
        elif tipo == "resgate":
            cat = "cadastros" if ev.get("tipo_recompensa") == "boas_vindas" else "resgates"
        else:
            continue
        s = stats.setdefault(g, {
            "cadastros_total": 0, "cadastros_janela": 0,
            "consumos_total": 0, "consumos_janela": 0,
            "resgates_total": 0, "resgates_janela": 0,
        })
        s[cat + "_total"] += 1
        if (ev.get("data") or "") >= corte:
            s[cat + "_janela"] += 1

    return jsonify({"dias": dias, "garcons": stats})


@app.route("/admin/push-lote", methods=["POST"])
def push_lote():
    """
    Dispara push para uma lista de telefones.
    Body: {"telefones": ["5521...", ...], "texto": "..."}
    """
    dados = request.get_json(silent=True) or {}
    telefones = dados.get("telefones") or []
    texto = (dados.get("texto") or "").strip()
    if not telefones or not texto:
        return jsonify({"erro": "telefones e texto obrigatorios"}), 400

    ok, com_push, sem_push, nao_encontrados = 0, 0, 0, []
    for tel in telefones:
        tel = so_digitos(str(tel))
        c = storage.carregar_consumidor(tel)
        if not c:
            nao_encontrados.append(tel)
            continue
        resultado = _notificar(c, texto)
        ok += 1
        if resultado and resultado.get("enviados", 0) > 0:
            com_push += 1
        else:
            sem_push += 1

    return jsonify({
        "status": "ok", "processados": ok,
        "com_dispositivo": com_push, "sem_dispositivo": sem_push,
        "nao_encontrados": nao_encontrados,
    })


@app.route("/q/<telefone>")
def qr_universal(telefone):
    """
    Destino do QR do cartao quando lido por uma CAMERA comum.
    Redireciona para a pagina de cadastro, com o bar do dono (indicador)
    e o dono como padrinho (member gets member).
    O scanner do garcom NAO passa por aqui — ele extrai o telefone da URL.
    """
    from urllib.parse import quote
    tel = so_digitos(telefone)
    c = storage.carregar_consumidor(tel)
    if not c:
        return Response("Cartao nao encontrado.", status=404, mimetype="text/plain")
    indicador = c.get("indicador", "") or ""
    destino = (f"https://hthoni.github.io/backbone/cadastro.html"
               f"?indicador={quote(indicador)}&padrinho={tel}")
    return Response(status=302, headers={"Location": destino})


@app.route("/admin/bares/stats")
def bares_stats():
    """
    Estatisticas por bar: associados (cadastros) e resgates,
    separando chopp de boas-vindas de chopp de meta atingida.
    ?dias=30 define a janela recente (default 30).
    """
    from datetime import timedelta
    dias = int(request.args.get("dias", 30))
    corte = (datetime.now(timezone.utc) - timedelta(days=dias)).isoformat()

    stats = {}
    def _s(bar):
        return stats.setdefault(bar or "—", {
            "associados_total": 0, "associados_janela": 0,
            "boasvindas_total": 0, "boasvindas_janela": 0,
            "metas_total": 0, "metas_janela": 0,
        })

    for ev in storage.listar_eventos():
        if ev.get("tipo") != "resgate":
            continue
        cat = "boasvindas" if ev.get("tipo_recompensa") == "boas_vindas" else "metas"
        e = _s(ev.get("bar"))
        e[cat + "_total"] += 1
        if (ev.get("data") or "") >= corte:
            e[cat + "_janela"] += 1

    for c in storage.listar_consumidores():
        e = _s(c.get("indicador"))
        e["associados_total"] += 1
        if (c.get("cadastro_em") or "") >= corte:
            e["associados_janela"] += 1

    return jsonify({"dias": dias, "bares": stats})


@app.route("/admin/bloqueados")
def admin_bloqueados():
    return jsonify(storage.carregar_bloqueados())


@app.route("/admin/bloqueados", methods=["POST"])
def admin_bloquear():
    """Body: {"telefones": [...], "bar": "<bar_id>" ou "*"} — bloqueia no bar indicado."""
    dados = request.get_json(silent=True) or {}
    bar_alvo = (dados.get("bar") or "*").strip() or "*"
    novos = [so_digitos(str(t)) for t in (dados.get("telefones") or [])]
    novos = [t for t in novos if 10 <= len(t) <= 13]
    lista = storage.carregar_bloqueados()
    lista.extend({"telefone": t, "bar": bar_alvo} for t in novos)
    storage.salvar_bloqueados(lista)
    return jsonify({"status": "ok", "adicionados": len(novos), "bar": bar_alvo})


@app.route("/admin/bloqueados/<telefone>", methods=["DELETE"])
def admin_desbloquear(telefone):
    """?bar=<bar_id> remove o bloqueio daquele bar; sem bar, remove todos do numero."""
    tel = so_digitos(telefone)
    bar_alvo = request.args.get("bar")
    lista = storage.carregar_bloqueados()
    if bar_alvo:
        lista = [b for b in lista if not (b["telefone"] == tel and b["bar"] == bar_alvo)]
    else:
        lista = [b for b in lista if b["telefone"] != tel]
    storage.salvar_bloqueados(lista)
    return jsonify({"status": "ok", "total": len(lista)})


@app.route("/bar/<bar_id>/atividade")
def atividade_bar(bar_id):
    """
    Feed de leituras do bar — usado na aba Atividade do scanner e no
    painel do gestor (aba Atividade e detalhe por garcom).

    ?limite=30      (max 100, default 30 — bloco do scroll infinito)
    ?offset=0       (posicao inicial; manda offset+limite do bloco anterior
                     para carregar o proximo)
    ?mes=2026-07    (opcional; filtra pelo mes do evento)
    ?garcom_id=g01  (opcional; filtra por garcom — usado na tela de
                     detalhe do garcom, aberta a partir do card na aba Atual)

    Resposta: {eventos:[...], offset, limite, total, tem_mais}
    tem_mais indica se ha mais eventos para o proximo bloco.
    """
    limite = min(int(request.args.get("limite", 30)), 100)
    offset = max(int(request.args.get("offset", 0)), 0)
    mes = request.args.get("mes")
    garcom_filtro = request.args.get("garcom_id")

    eventos = [e for e in storage.listar_eventos()
               if (bar_id == "*" or e.get("bar") == bar_id) and e.get("tipo") in ("punch", "resgate")]

    if mes:
        eventos = [e for e in eventos if (e.get("data") or "").startswith(mes)]
    if garcom_filtro:
        eventos = [e for e in eventos if e.get("garcom_id") == garcom_filtro]

    eventos.sort(key=lambda e: e.get("data", ""), reverse=True)
    total = len(eventos)
    pagina = eventos[offset:offset + limite]

    nomes = {}
    for tel in {e.get("telefone") for e in pagina if e.get("telefone")}:
        c = storage.carregar_consumidor(tel)
        if c:
            nomes[tel] = c.get("nome", "")

    garcons = {g["id"]: g.get("nome", g["id"]) for g in storage.listar_garcons()}
    garcons.update({g["id"]: g.get("nome", g["id"]) for g in storage.listar_gestores()})
    garcons.update({a["id"]: a.get("nome", a["id"]) for a in storage.listar_admins()})

    return jsonify({
        "eventos": [{
            "nome": nomes.get(e.get("telefone")) or e.get("telefone") or "—",
            "data": e.get("data"),
            "garcom": garcons.get(e.get("garcom_id"), e.get("garcom_id") or "—"),
            "tipo": e.get("tipo"),
            "tipo_recompensa": e.get("tipo_recompensa"),
        } for e in pagina],
        "offset": offset,
        "limite": limite,
        "total": total,
        "tem_mais": (offset + limite) < total,
    })


@app.route("/bar/<bar_id>/resumo")
def bar_resumo(bar_id):
    """
    Resumo do bar num periodo + quebra por garcom.
    Alimenta as abas Atual (mes corrente) e Historico (mes selecionado)
    do painel do gestor.

    ?mes=2026-07  (default: mes corrente, formato YYYY-MM)

    Categorias (mesma logica das outras estatisticas do admin):
      cadastros -> resgates de boas_vindas (chopp de boas-vindas)
      pontuacao -> punches (chopps de rotina)
      resgates  -> resgates de premio (meta de 7/10 atingida)
    """
    mes = request.args.get("mes") or datetime.now(timezone.utc).strftime("%Y-%m")

    garcons_map = {g["id"]: g.get("nome", g["id"]) for g in storage.listar_garcons()}
    garcons_map.update({g["id"]: g.get("nome", g["id"]) for g in storage.listar_gestores()})
    garcons_map.update({a["id"]: a.get("nome", a["id"]) for a in storage.listar_admins()})

    totais = {"cadastros": 0, "pontuacao": 0, "resgates": 0}
    por_garcom = {}

    # todo garcom do bar aparece desde ja, mesmo com zero atividade —
    # senao um garcom recem-criado some da lista ate escanear o 1o chopp
    if bar_id != "*":
        for g in storage.listar_garcons():
            if g.get("bar_id") == bar_id:
                por_garcom[g["id"]] = {
                    "garcom_id": g["id"], "nome": g.get("nome", g["id"]),
                    "cadastros": 0, "pontuacao": 0, "resgates": 0,
                }

    for ev in storage.listar_eventos():
        if bar_id != "*" and ev.get("bar") != bar_id:
            continue
        if not (ev.get("data") or "").startswith(mes):
            continue

        tipo = ev.get("tipo")
        if tipo == "punch":
            cat = "pontuacao"
        elif tipo == "resgate":
            cat = "cadastros" if ev.get("tipo_recompensa") == "boas_vindas" else "resgates"
        else:
            continue

        totais[cat] += 1

        gid = ev.get("garcom_id")
        if gid:
            g = por_garcom.setdefault(gid, {
                "garcom_id": gid, "nome": garcons_map.get(gid, gid),
                "cadastros": 0, "pontuacao": 0, "resgates": 0,
            })
            g[cat] += 1

    return jsonify({
        "bar_id": bar_id,
        "mes": mes,
        "totais": totais,
        "garcons": sorted(por_garcom.values(), key=lambda g: g["nome"]),
    })


@app.route("/bar/<bar_id>/historico")
def bar_historico(bar_id):
    """
    Alimenta a aba Historico do painel do gestor numa chamada so:
    o total geral do bar (todos os meses) + a lista de meses com
    totais, do mais recente para o mais antigo — cada item vira
    direto um card na tela, sem precisar de 1 request por mes.
    """
    totais_geral = {"cadastros": 0, "pontuacao": 0, "resgates": 0}
    por_mes = {}

    for ev in storage.listar_eventos():
        if bar_id != "*" and ev.get("bar") != bar_id:
            continue
        data = ev.get("data") or ""
        if len(data) < 7:
            continue
        mes = data[:7]  # YYYY-MM

        tipo = ev.get("tipo")
        if tipo == "punch":
            cat = "pontuacao"
        elif tipo == "resgate":
            cat = "cadastros" if ev.get("tipo_recompensa") == "boas_vindas" else "resgates"
        else:
            continue

        totais_geral[cat] += 1
        m = por_mes.setdefault(mes, {"mes": mes, "cadastros": 0, "pontuacao": 0, "resgates": 0})
        m[cat] += 1

    meses = sorted(por_mes.values(), key=lambda m: m["mes"], reverse=True)

    return jsonify({
        "bar_id": bar_id,
        "totais": totais_geral,
        "meses": meses,
    })


@app.route("/")
def health():
    return jsonify({"status": "ok", "sistema": "Clube Backbone", "versao": 3})


# ══════════════════════════════════════════════════════════════
#  EXTRAIR PADRINHO  (member gets member via BotConversa)
#  Recebe a mensagem suja e devolve so o telefone do padrinho.
# ══════════════════════════════════════════════════════════════

@app.route("/extrair-padrinho", methods=["POST", "GET"])
def extrair_padrinho():
    msg = ""
    if request.is_json:
        msg = (request.get_json(silent=True) or {}).get("mensagem", "")
    if not msg:
        msg = request.form.get("mensagem", "") or request.args.get("mensagem", "")

    padrinho = ""
    if msg:
        m = re.search(r"padrinho[^0-9]*([0-9][0-9\s\-\+\(\)]{9,})", msg, re.IGNORECASE)
        if m:
            padrinho = so_digitos(m.group(1))
        else:
            achados = re.findall(r"[0-9][0-9\s\-\+\(\)]{9,}", msg)
            cands = [so_digitos(a) for a in achados]
            cands = [c for c in cands if 10 <= len(c) <= 13]
            if cands:
                padrinho = max(cands, key=len)

    return jsonify({"padrinho": padrinho})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=True)
