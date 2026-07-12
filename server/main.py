# Backbone Beer — Sistema de Fidelização de Consumidores.

import os
import json
import uuid
from datetime import datetime, timezone
from flask import Flask, request, jsonify
from flask_cors import CORS
import storage

app = Flask(__name__)
CORS(app)

@app.route("/cadastro/<bar>/<telefone>/<nome>/<dia>/<mes>/<ano>/<time>")
def cadastro(bar, telefone, nome, dia, mes, ano, time):
    from urllib.parse import unquote
    nome = unquote(nome)
    bar  = unquote(bar)
    time = unquote(time)

    existente = storage.carregar_consumidor(telefone)
    if existente:
        return jsonify({"status": "ja_cadastrado", "consumidor": existente})

    consumidor = {
        "telefone":      telefone,
        "nome":          nome,
        "bar_origem":    bar,
        "nascimento_dia": dia,
        "nascimento_mes": mes,
        "nascimento_ano": ano,
        "time":          time,
        "punches":       0,
        "recompensas_disponiveis": 1,
        "recompensas_total": 0,
        "ultimo_punch":  None,
        "cadastro_em":   datetime.now(timezone.utc).isoformat(),
        "ativo":         True
    }

    storage.salvar_consumidor(consumidor)

    evento = {
        "id":        str(uuid.uuid4()),
        "tipo":      "cadastro",
        "telefone":  telefone,
        "bar":       bar,
        "garcom_id": None,
        "data":      datetime.now(timezone.utc).isoformat()
    }
    storage.salvar_evento(evento)

    return jsonify({"status": "cadastrado", "consumidor": consumidor})

@app.route("/consumidor/<telefone>")
def consultar_consumidor(telefone):
    c = storage.carregar_consumidor(telefone)
    if not c:
        return jsonify({"erro": "nao_encontrado"}), 404
    config = storage.carregar_config()
    c["punches_para_recompensa"] = config["punches_para_recompensa"]
    return jsonify(c)

@app.route("/punch", methods=["POST"])
def punch():
    dados = request.get_json()
    telefone  = dados.get("telefone")
    garcom_id = dados.get("garcom_id")
    bar       = dados.get("bar")

    if not telefone or not garcom_id or not bar:
        return jsonify({"erro": "dados_incompletos"}), 400

    consumidor = storage.carregar_consumidor(telefone)
    if not consumidor:
        return jsonify({"erro": "consumidor_nao_encontrado"}), 404

    garcom = storage.carregar_garcom(garcom_id)
    if not garcom:
        return jsonify({"erro": "garcom_nao_encontrado"}), 404

    if garcom.get("bar_id") != bar:
        return jsonify({"erro": "bar_diferente"}), 403

    config = storage.carregar_config()
    punches_para_recompensa = config.get("punches_para_recompensa", 10)

    consumidor["punches"] = consumidor.get("punches", 0) + 1
    consumidor["ultimo_punch"] = datetime.now(timezone.utc).isoformat()

    recompensa_gerada = False
    if consumidor["punches"] >=
