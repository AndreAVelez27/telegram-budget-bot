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


# ── Movimientos de bolsillos ──────────────────────────────────────────────────
# Historial de ingresos/retiros de cada bolsillo (como "Últimos movimientos" en
# la app de bolsillos). Vive en una subcolección aparte, no dentro del documento
# de estado: crece sin límite y el documento de Firestore tiene tope de 1 MB.

def registrar_movimiento(fecha: str, bolsillo: str, nombre: str, tipo: str, monto: int, nota: str = "") -> None:
    """tipo: 'ingreso' | 'retiro'. `bolsillo` es el slug; `nombre` el nombre visible."""
    (_cliente().collection(COLECCION).document(DOCUMENTO).collection("movimientos").add({
        "fecha": fecha,
        "bolsillo": bolsillo,
        "nombre": nombre,
        "tipo": tipo,
        "monto": monto,
        "nota": nota,
        "creado": firestore.SERVER_TIMESTAMP,
    }))


def leer_movimientos(limite: int = 20) -> list:
    consulta = (_cliente().collection(COLECCION).document(DOCUMENTO).collection("movimientos")
                .order_by("creado", direction=firestore.Query.DESCENDING).limit(limite))
    return [d.to_dict() for d in consulta.stream()]
