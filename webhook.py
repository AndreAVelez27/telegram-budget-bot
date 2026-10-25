"""Receptor de mensajes de Telegram para Cloud Run.

Telegram hace POST /webhook por cada mensaje; este servicio lo valida, lo pasa
por el mismo dispatcher que usa el polling (budget_bot.procesar_mensaje) y
guarda el estado en Firestore. Desplegar con --concurrency=1 y
--max-instances=1: el estado es un único documento que cada request lee,
modifica y reescribe, así que dos requests simultáneos pisarían sus cambios.
"""
import hmac
import os
import traceback

from fastapi import FastAPI, Header, HTTPException, Request

import budget_bot as bot
from estado_store import guardar_estado, leer_estado

app = FastAPI()
SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")


@app.get("/")
def salud() -> dict:
    return {"ok": True}


@app.post("/webhook")
async def webhook(request: Request, x_telegram_bot_api_secret_token: str = Header(default="")) -> dict:
    # Cloud Run es público (Telegram no soporta OAuth): el secreto es la única barrera.
    if not SECRET or not hmac.compare_digest(x_telegram_bot_api_secret_token, SECRET):
        raise HTTPException(status_code=403)

    update = await request.json()
    msg = update.get("message") or {}
    if str(msg.get("chat", {}).get("id")) != str(bot.CHAT_ID) or not msg.get("text"):
        return {"ok": True}  # otro chat, fotos, stickers…: se ignora (200 para que Telegram no reintente)

    estado = leer_estado()
    update_id = update["update_id"]
    if update_id <= estado.get("ultimo_update_id", 0):
        return {"ok": True}  # reintento de Telegram de un mensaje ya procesado

    # Se guarda ANTES de procesar: si algo tarda y Telegram reintenta, el
    # reintento ya se ve como duplicado y no registra el gasto dos veces.
    estado["ultimo_update_id"] = update_id
    guardar_estado(estado)

    try:
        await bot.procesar_mensaje(estado, msg["text"])
    except Exception:
        traceback.print_exc()
        await bot.enviar_mensaje("⚠️ Algo falló procesando ese mensaje. Revisa si quedó registrado y reintenta.")
    finally:
        guardar_estado(estado)
    return {"ok": True}
