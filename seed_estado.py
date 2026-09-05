"""Migración única: copia state.json a Firestore.

Correr una sola vez, localmente, después de completar los pasos manuales de
GCP (proyecto, Firestore API+DB habilitados, rol datastore.user otorgado a
la service account). Requiere .env con GOOGLE_CREDENTIALS_B64 apuntando a esa
misma service account (o exportar la variable en la shell antes de correrlo).

Se puede borrar después de usarlo una vez.
"""
import json

from dotenv import load_dotenv

from estado_store import guardar_estado, leer_estado

load_dotenv()

with open("state.json") as f:
    estado_local = json.load(f)

guardar_estado(estado_local)

estado_firestore = leer_estado()
assert estado_firestore == estado_local, "El estado leído de Firestore no coincide con state.json"

print("✅ Estado sembrado en Firestore y verificado.")
print(f"   estado={estado_firestore.get('estado')!r}, fecha_nomina={estado_firestore.get('fecha_nomina')!r}")
