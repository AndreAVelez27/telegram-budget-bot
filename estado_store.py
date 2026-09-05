import base64
import copy
import json
import os

from google.cloud import firestore
from google.oauth2.service_account import Credentials

COLECCION = "budget_bot"
DOCUMENTO = "estado"


def _cliente() -> firestore.Client:
    raw = os.getenv("GOOGLE_CREDENTIALS_B64")
    decoded_credentials = base64.b64decode(raw).decode("utf-8")
    credentials_dict = json.loads(decoded_credentials)
    creds = Credentials.from_service_account_info(credentials_dict)
    return firestore.Client(project=credentials_dict["project_id"], credentials=creds)


def _decodificar_items(estado: dict) -> dict:
    """Firestore no permite arrays anidados dentro de arrays; semana['items']
    se guarda como lista de mapas {nombre,valor,medio} y se reconstruye aquí
    como lista de listas para que el resto de budget_bot.py no cambie."""
    for semana in estado.get("semanas", []):
        if semana.get("items") and isinstance(semana["items"][0], dict):
            semana["items"] = [
                [item["nombre"], item["valor"], item["medio"]] for item in semana["items"]
            ]
    return estado


def _codificar_items(estado: dict) -> dict:
    estado = copy.deepcopy(estado)
    for semana in estado.get("semanas", []):
        if semana.get("items") and not isinstance(semana["items"][0], dict):
            semana["items"] = [
                {"nombre": nombre, "valor": valor, "medio": medio}
                for nombre, valor, medio in semana["items"]
            ]
    return estado


def leer_estado() -> dict:
    doc = _cliente().collection(COLECCION).document(DOCUMENTO).get()
    if doc.exists:
        return _decodificar_items(doc.to_dict())
    return {"estado": "idle", "ultimo_update_id": 0}


def guardar_estado(estado: dict) -> None:
    _cliente().collection(COLECCION).document(DOCUMENTO).set(_codificar_items(estado))
