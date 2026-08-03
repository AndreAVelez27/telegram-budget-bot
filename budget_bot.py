import asyncio
import json
import re
from datetime import date, timedelta
from typing import Optional
import os
import sys
import httpx
import holidays
from dotenv import load_dotenv
from sheets import registrar_gasto

load_dotenv()

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
BASE_URL = f"https://api.telegram.org/bot{TOKEN}"
STATE_FILE = "state.json"

# ── Estado ────────────────────────────────────────────────────────────────────
# state.json guarda en qué paso del flujo conversacional estamos.
# Estructura:
#   estado:          "idle" | "esperando_remanente" | "listo"
#   ultimo_update_id: el último mensaje de Telegram que ya procesamos
#   fecha_nomina:    fecha del último día de nómina procesado (evita repetir)
#   remanente:       valor confirmado por el usuario

def leer_estado() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"estado": "idle", "ultimo_update_id": 0}

def guardar_estado(estado: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(estado, f, indent=2, default=str)

# ── Fechas ────────────────────────────────────────────────────────────────────

def dia_nomina(mes: int, año: int) -> date:
    """Último día hábil colombiano en o antes del 27 del mes dado."""
    festivos = holidays.Colombia(years=año)
    candidato = date(año, mes, 27)
    while candidato.weekday() >= 5 or candidato in festivos:
        candidato -= timedelta(days=1)
    return candidato

def _fmt(d: date) -> str:
    meses = ["","Ene","Feb","Mar","Abr","May","Jun",
             "Jul","Ago","Sep","Oct","Nov","Dic"]
    return f"{d.day} {meses[d.month]}"

# ── Cálculo dinámico del ciclo ────────────────────────────────────────────────
# Dado un remanente, distribuye el presupuesto en 4 semanas + cierre.
# Porcentajes basados en el ciclo actual como referencia:
#   - Cada semana normal: ~22.5% del total
#   - Cierre (~4 días):   ~10% del total (lo que sobre de las 4 semanas)
# Dentro de cada semana:
#   - Domicilios: 38.6%, Salidas: 24.1%, Transporte: 19.3%, Reserva: 18.1%

def calcular_semanas(remanente: int, fecha_pago: date) -> list[dict]:
    total = (remanente // 1000) * 1000

    p_semana = round(total * 0.225 / 1000) * 1000
    p_cierre  = total - 4 * p_semana

    tips = [
        "Arranque limpio del ciclo. ¡Cabeza fría! 💪",
        "Semana del capricho 🎉 Solo UN capricho.",
        "Semana estándar. La reserva se acumula para el cierre 🏁",
        "Recta final. No arranques caprichos nuevos 🙏",
    ]

    semanas = []
    inicio = fecha_pago

    for i in range(4):
        fin = inicio + timedelta(days=6)
        es_capricho = (i == 1)

        d = round(p_semana * 0.386 / 1000) * 1000
        t = round(p_semana * 0.193 / 1000) * 1000
        s = round(p_semana * 0.241 / 1000) * 1000
        r = p_semana - d - t - s  # el resto evita errores de redondeo

        semanas.append({
            "label": f"Semana {i + 1}",
            "fechas": f"{_fmt(inicio)} – {_fmt(fin)}",
            "presupuesto": p_semana,
            "items": [
                ("🛵 Domicilios / delivery", d, "Rappicard"),
                ("☕ Salidas / planes",       s, "Rappicard"),
                ("🚌 Colchón transporte",     t, "Efectivo / Rappicard"),
                ("✨ Capricho" if es_capricho else "🐷 Reserva de la semana",
                 r,
                 "Rappicard o efectivo" if es_capricho else "Guardar"),
            ],
            "tip": tips[i],
            "fecha_inicio": str(inicio),
            "fecha_fin": str(fin),
        })
        inicio = fin + timedelta(days=1)

    # Fecha fin del ciclo = día de nómina del mes siguiente
    mes_sig = fecha_pago.month % 12 + 1
    año_sig = fecha_pago.year + (1 if fecha_pago.month == 12 else 0)
    fin_ciclo = dia_nomina(mes_sig, año_sig)

    d_c = round(p_cierre * 0.44 / 1000) * 1000
    s_c = round(p_cierre * 0.22 / 1000) * 1000
    t_c = round(p_cierre * 0.17 / 1000) * 1000
    r_c = p_cierre - d_c - s_c - t_c

    semanas.append({
        "label": "Cierre del ciclo",
        "fechas": f"{_fmt(inicio)} – {_fmt(fin_ciclo)}",
        "presupuesto": p_cierre,
        "items": [
            ("🛵 Domicilios básicos", d_c, "Rappicard"),
            ("☕ Salidas mínimas",    s_c, "Rappicard"),
            ("🚌 Transporte",         t_c, "Efectivo"),
            ("🐷 Colchón final",      r_c, "Guardar"),
        ],
        "tip": "Modo conservador: llega limpia al próximo pago 💚",
        "fecha_inicio": str(inicio),
        "fecha_fin": str(fin_ciclo),
    })

    return semanas

# ── Mensajes ──────────────────────────────────────────────────────────────────

def construir_mensaje_semana(s: dict, total_ciclo: int, fecha_pago: date) -> str:
    mes_sig = fecha_pago.month % 12 + 1
    año_sig = fecha_pago.year + (1 if fecha_pago.month == 12 else 0)
    fin_ciclo = dia_nomina(mes_sig, año_sig)

    lineas = [
        f"💚 *Presupuesto semanal — {s['label']}*",
        f"📅 {s['fechas']}",
        f"💰 Total disponible: *${s['presupuesto']:,}*\n".replace(",", "."),
        "━━━━━━━━━━━━━━━━━━",
    ]
    for nombre, valor, medio in s["items"]:
        lineas.append(f"{nombre}\n   `${valor:,}` · _{medio}_".replace(",", "."))
    lineas += [
        "━━━━━━━━━━━━━━━━━━",
        f"💡 {s['tip']}",
        "",
        "🃏 *Recuerda:* Rappicard primero (1% cashback).",
        f"\n_Ciclo: {_fmt(fecha_pago)} → {_fmt(fin_ciclo)} · ${total_ciclo:,} total_".replace(",", "."),
    ]
    return "\n".join(lineas)

def construir_resumen_ciclo(estado: dict) -> str:
    semanas = estado["semanas"]
    fecha_pago = date.fromisoformat(estado["fecha_nomina"])
    total = sum(s["presupuesto"] for s in semanas)
    lineas = [
        f"📊 *Resumen del ciclo — {_fmt(fecha_pago)} → {_fmt(date.fromisoformat(semanas[-1]['fecha_fin']))}*",
        "━━━━━━━━━━━━━━━━━━",
    ]
    for s in semanas:
        lineas.append(
            f"📅 *{s['label']}* ({s['fechas']})\n"
            f"   💰 `${s['presupuesto']:,}` · gastado `${s['gastado']:,}` · saldo `${s['saldo']:,}`"
            .replace(",", ".")
        )
    lineas += [
        "━━━━━━━━━━━━━━━━━━",
        f"💵 *Total del ciclo: ${total:,}*".replace(",", "."),
        "",
        "_Rappicard primero · Reservas al lado · Sin caprichos de más_ 💚",
    ]
    return "\n".join(lineas)

# ── Telegram API ──────────────────────────────────────────────────────────────

async def enviar_mensaje(texto: str) -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{BASE_URL}/sendMessage",
            json={"chat_id": CHAT_ID, "text": texto, "parse_mode": "Markdown"},
            timeout=10,
        )
        return resp.json()

async def obtener_updates(client: httpx.AsyncClient, offset: int) -> list:
    """Long polling: Telegram sostiene la conexión hasta 30s esperando mensajes."""
    resp = await client.get(
        f"{BASE_URL}/getUpdates",
        params={"offset": offset, "timeout": 30},
        timeout=35,  # siempre mayor que el timeout de Telegram
    )
    data = resp.json()
    return data.get("result", []) if data.get("ok") else []

def parsear_monto(texto: str) -> Optional[int]:
    """Extrae un entero de texto libre. Ej: '$1.500.000' → 1500000."""
    limpio = re.sub(r"[$.\s,_]", "", texto.strip())
    limpio = re.sub(r"[^\d]", "", limpio)
    return int(limpio) if limpio else None

# ── Flujo de nómina ───────────────────────────────────────────────────────────

async def flujo_nomina(hoy: date) -> None:
    estado = leer_estado()

    # Si ya configuramos el ciclo para esta nómina, no repetir
    if estado.get("estado") == "listo" and estado.get("fecha_nomina") == str(hoy):
        print("✅ Ciclo ya configurado para esta nómina. Sin acción.")
        return

    # Pregunta inicial
    await enviar_mensaje(
        "💰 *¡Hoy es día de nómina!*\n\n"
        "¿Cuánto te quedó de remanente este mes?\n"
        "_(Escríbelo como número, ej: 1500000)_"
    )
    estado["estado"] = "esperando_remanente"
    estado["fecha_nomina"] = str(hoy)
    guardar_estado(estado)
    print("⏳ Esperando respuesta del usuario (máx. 10 min)...")

    # Long polling: espera hasta 10 minutos una respuesta válida
    deadline = asyncio.get_event_loop().time() + 600
    offset = estado.get("ultimo_update_id", 0) + 1

    async with httpx.AsyncClient() as client:
        while asyncio.get_event_loop().time() < deadline:
            updates = await obtener_updates(client, offset)

            for update in updates:
                offset = update["update_id"] + 1
                estado["ultimo_update_id"] = update["update_id"]
                guardar_estado(estado)

                msg = update.get("message", {})
                # Solo procesa mensajes del chat correcto
                if str(msg.get("chat", {}).get("id")) != str(CHAT_ID):
                    continue

                texto = msg.get("text", "")
                remanente = parsear_monto(texto)

                if remanente is None or remanente < 10000:
                    await enviar_mensaje(
                        "No entendí el monto 😅\n"
                        "Escríbelo como número: ej. *1500000* o *1.500.000*"
                    )
                    continue

                # Monto válido: calcular y enviar el plan del ciclo
                semanas = calcular_semanas(remanente, hoy)
                for s in semanas:
                    s["gastado"] = 0
                    s["saldo"] = s["presupuesto"]
                total = sum(s["presupuesto"] for s in semanas)

                await enviar_mensaje(
                    f"✅ Remanente registrado: *${remanente:,}*\n"
                    f"Te mando el plan del ciclo ahora 👇".replace(",", ".")
                )

                for semana in semanas:
                    await enviar_mensaje(construir_mensaje_semana(semana, total, hoy))

                estado["estado"] = "listo"
                estado["remanente"] = remanente
                estado["semanas"] = semanas
                guardar_estado(estado)
                print(f"✅ Ciclo configurado con remanente ${remanente:,}")
                return

    print("⏰ Timeout: el usuario no respondió en 10 minutos.")


# ── Flujo de gastos ──────────────────────────────────────────────────────────────────

def es_reporte_gasto(texto:str) -> bool:
    """ Detecta si el mensaje es un reporte de gasto y devuelve True/False"""
    return bool(re.search(r"hoy gasté|hoy pedí|hoy compré|hoy gaste|hoy pedi|hoy compre", texto, re.IGNORECASE))


async def esperar_respuesta(estado: dict, timeout: int = 300) -> Optional[str]:
    deadline = asyncio.get_event_loop().time() + timeout
    offset = estado.get("ultimo_update_id", 0) + 1

    async with httpx.AsyncClient() as client:
        while asyncio.get_event_loop().time() < deadline:
            updates = await obtener_updates(client, offset)

            for update in updates:
                offset = update["update_id"] + 1
                estado["ultimo_update_id"] = update["update_id"]
                guardar_estado(estado)

                msg = update.get("message", {})
                if str(msg.get("chat", {}).get("id")) != str(CHAT_ID):
                    continue

                return msg.get("text","")

    return None

async def flujo_gastos(estado: dict) -> None:
    await enviar_mensaje("¿Cuánto gastaste?")
    monto_texto = await esperar_respuesta(estado)
    monto = parsear_monto(monto_texto)

    await enviar_mensaje("¿En qué categoría?\n_domicilios / salidas / transporte / capricho_")
    categoria = await esperar_respuesta(estado)

    await enviar_mensaje("¿Con qué medio?\n_Rappicard / efectivo_")
    medio = await esperar_respuesta(estado)
    
    registrar_gasto(str(date.today()), monto, categoria, medio)
    await enviar_mensaje(f"✅ Registrado: ${monto:,} en {categoria} con {medio}".replace(",","."))

    semana = semana_en_curso(estado)
    if semana:
        semana["gastado"] += monto
        semana["saldo"] = semana["presupuesto"] - semana["gastado"]
        guardar_estado(estado)
        await enviar_mensaje(
            f"📊 Saldo restante esta semana: *${semana['saldo']:,}*"
        )
        if semana["saldo"] < 0:
            await flujo_buffer(estado, semana)

# ── Buffer ────────────────────────────────────────────────────────────────────

async def flujo_buffer(estado: dict, semana: dict) -> None:
    await enviar_mensaje(
        f"⚠️ Te pasaste del presupuesto esta semana.\n"
        f"¿Cuánto buffer tomamos de la semana siguiente?\n"
        f"_50000 / 100000 / 150000_"
    )

    respuesta = await esperar_respuesta(estado)
    buffer = parsear_monto(respuesta)

    if buffer not in [50000, 100000, 150000]:
        await enviar_mensaje("Monto no válido. Elige 50000, 100000 o 150000.")
        return

    semanas = estado["semanas"]
    semana_siguiente = None
    for i, s in enumerate(semanas):
        if s == semana and i + 1 < len(semanas):
            semana_siguiente = semanas[i + 1]
            break

    if semana_siguiente is None:
        await enviar_mensaje("No hay semana siguiente para tomar buffer 😔")
        return

    semana["saldo"] += buffer
    semana_siguiente["presupuesto"] -= buffer
    semana_siguiente["saldo"] -= buffer
    guardar_estado(estado)

    await enviar_mensaje(
        f"✅ Buffer aplicado: *+${buffer:,}*\n"
        f"Saldo semana actual: *${semana['saldo']:,}*\n"
        f"Presupuesto semana siguiente: *${semana_siguiente['presupuesto']:,}*"
        .replace(",", ".")
    )

# ── Semana en curso ───────────────────────────────────────────────────────────
def semana_en_curso(estado:dict):
    hoy = str(date.today())
    for s in estado.get("semanas",[]):
        if s["fecha_inicio"] <= hoy <= s["fecha_fin"]:
            return s

# ── Funcion de parseo ───────────────────────────────────────────────────────────

CATEGORIAS = {
    "domicilios": ["domicilio", "domicilios", "rappi", "delivery", "pedido"],
    "salidas":    ["salida", "salidas", "cafe", "café", "restaurante", "plan", "planes"],
    "transporte": ["transporte", "bus", "taxi", "uber", "metro", "trayecto"],
    "capricho":   ["capricho", "compra", "jean", "ropa", "maquillaje"],
    "mercado":    ["mercado", "super", "supermercado", "droguería", "drogueria"],
    "familia":    ["familia", "casa", "apto", "apartamento"],
    "mascota":    ["mascota", "veterinaria", "veterinario", "perro", "gato"],
}

MEDIOS = {
    "rappicard": ["rappicard", "tarjeta", "credito", "crédito"],
    "efectivo":  ["efectivo", "cash", "billete"],
}

def parsear_gasto_texto(texto: str) -> tuple:
    monto = parsear_monto(texto)

    # Palabras completas del mensaje: evita que "rappicard" active el sinónimo "rappi"
    palabras = set(re.findall(r"[a-záéíóúñü]+", texto.lower()))

    medio = None
    for med, sinonimos in MEDIOS.items():
        if palabras & set(sinonimos):
            medio = med
            break

    categoria = None
    for cat, sinonimos in CATEGORIAS.items():
        if palabras & set(sinonimos):
            categoria = cat
            break

    return (monto, categoria, medio)
 

# ── Flujo semanal ─────────────────────────────────────────────────────────────
# Corre todos los días: solo actúa si hoy es fecha_inicio de una semana del ciclo.
# La Semana 1 se excluye porque flujo_nomina ya envía el plan completo ese día.

async def flujo_semanal() -> None:
    estado = leer_estado()
    if estado.get("estado") != "listo":
        print("⏭️  Ciclo no inicializado. Sin acción.")
        return

    hoy = str(date.today())
    semanas = estado.get("semanas", [])

    idx = None
    for i, s in enumerate(semanas):
        if s["fecha_inicio"] == hoy:
            idx = i
            break

    if idx is None or idx == 0:
        print(f"⏭️  Hoy ({hoy}) no empieza ninguna semana. Sin acción.")
        return

    semana = semanas[idx]
    anterior = semanas[idx - 1]
    fecha_pago = date.fromisoformat(estado["fecha_nomina"])
    total = sum(s["presupuesto"] for s in semanas)

    await enviar_mensaje(construir_mensaje_semana(semana, total, fecha_pago))

    # Pregunta por el remanente de la semana que acaba de terminar
    await enviar_mensaje(
        f"🔄 ¿Te quedó remanente de la *{anterior['label']}*?\n"
        f"_(según mis cuentas quedó ${anterior['saldo']:,})_\n\n"
        f"Escribe el monto para sumarlo a esta semana, o *no* si no quedó nada."
        .replace(",", ".")
    )

    respuesta = await esperar_respuesta(estado, timeout=600)

    if respuesta is None:
        guardar_estado(estado)
        print("⏰ Timeout: sin respuesta sobre el remanente.")
        return

    remanente = parsear_monto(respuesta)

    if remanente:
        semana["presupuesto"] += remanente
        semana["saldo"] += remanente
        guardar_estado(estado)
        await enviar_mensaje(
            f"✅ Remanente de *${remanente:,}* sumado.\n"
            f"💰 Presupuesto de esta semana: *${semana['presupuesto']:,}*"
            .replace(",", ".")
        )
        print(f"✅ Remanente ${remanente:,} sumado a {semana['label']}")
    else:
        guardar_estado(estado)
        await enviar_mensaje("👌 Listo, seguimos con el presupuesto normal de la semana.")
        print("✅ Sin remanente que sumar.")

# ── Escucha de gastos ─────────────────────────────────────────────────────────

async def flujo_escucha() -> None:
    estado = leer_estado()
    if estado.get("estado") != "listo":
        print("⏭️  Ciclo no inicializado. Sin acción.")
        return

    offset = estado.get("ultimo_update_id", 0) + 1
    gastos_registrados = []

    async with httpx.AsyncClient() as client:
        updates = await obtener_updates(client, offset)

        for update in updates:
            estado["ultimo_update_id"] = update["update_id"]
            offset = update["update_id"] + 1

            msg = update.get("message", {})
            if str(msg.get("chat", {}).get("id")) != str(CHAT_ID):
                continue

            texto = msg.get("text", "")
            if not es_reporte_gasto(texto):
                continue

            monto, categoria, medio = parsear_gasto_texto(texto)
            if not monto or not categoria or not medio:
                await enviar_mensaje(
                    f"No entendí ese gasto 😅\n"
                    f"Formato: _hoy gasté 25000 salidas rappicard_"
                )
                continue

            registrar_gasto(str(date.today()), monto, categoria, medio)

            semana = semana_en_curso(estado)
            if semana:
                semana["gastado"] += monto
                semana["saldo"] = semana["presupuesto"] - semana["gastado"]

            gastos_registrados.append((monto, categoria, medio))

    guardar_estado(estado)

    if not gastos_registrados:
        print("📭 Sin mensajes de gasto pendientes.")
        return

    lineas = ["📋 *Gastos registrados:*"]
    for monto, categoria, medio in gastos_registrados:
        lineas.append(f"   · `${monto:,}` · {categoria} · {medio}".replace(",", "."))

    semana = semana_en_curso(estado)
    if semana:
        lineas.append(f"\n📊 Saldo restante esta semana: *${semana['saldo']:,}*".replace(",", "."))
        if semana["saldo"] < 0:
            await enviar_mensaje("\n".join(lineas))
            await flujo_buffer(estado, semana)
            return

    await enviar_mensaje("\n".join(lineas))
    print(f"✅ {len(gastos_registrados)} gasto(s) procesado(s)")

# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    hoy = date.today()

    if "--nomina" in sys.argv:
        nomina = dia_nomina(hoy.month, hoy.year)
        if hoy != nomina:
            print(f"⏭️  Hoy ({hoy}) no es día de nómina ({nomina}). Sin acción.")
            return
        await flujo_nomina(hoy)

    elif "--escucha" in sys.argv:
        await flujo_escucha()

    elif "--resumen" in sys.argv:
        estado = leer_estado()
        if not estado.get("semanas"):
            print("⏭️  No hay ciclo configurado. Sin acción.")
            return
        mensaje = construir_resumen_ciclo(estado)
        resultado = await enviar_mensaje(mensaje)
        if resultado.get("ok"):
            print("✅ Resumen del ciclo enviado")
        else:
            print(f"❌ Error: {resultado}")

    elif "--gasto" in sys.argv:
        estado = leer_estado()
        await flujo_gastos(estado)

    else:
        await flujo_semanal()

if __name__ == "__main__":
    asyncio.run(main())
