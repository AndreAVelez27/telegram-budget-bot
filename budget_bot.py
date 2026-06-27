import asyncio
from datetime import date
import os
import sys
import httpx
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

BASE_URL = f"https://api.telegram.org/bot{TOKEN}"

# Presupuesto del ciclo 26 Jun – 27 Jul
SEMANAS = [
    {
        "label": "Semana 1",
        "fechas": "27 Jun – 3 Jul",
        "presupuesto": 415000,
        "items": [
            ("🛵 Domicilios / delivery",   160000, "Rappicard"),
            ("☕ Salidas / planes",         100000, "Rappicard"),
            ("🚌 Colchón transporte",        80000, "Efectivo / Rappicard"),
            ("🐷 Reserva de la semana",      75000, "Guardar"),
        ],
        "tip": "Arranque limpio del ciclo. Sin caprichos esta semana 💪",
    },
    {
        "label": "Semana 2",
        "fechas": "4 Jul – 10 Jul",
        "presupuesto": 415000,
        "items": [
            ("🛵 Domicilios / delivery",   160000, "Rappicard"),
            ("☕ Salidas / planes",          80000, "Rappicard"),
            ("🚌 Colchón transporte",        60000, "Efectivo / Rappicard"),
            ("✨ Capricho (jean/maquillaje)",115000, "Rappicard o efectivo"),
        ],
        "tip": "Semana del capricho 🎉 Recuerda: max $120k y solo UN capricho.",
    },
    {
        "label": "Semana 3",
        "fechas": "11 Jul – 17 Jul",
        "presupuesto": 415000,
        "items": [
            ("🛵 Domicilios / delivery",   160000, "Rappicard"),
            ("☕ Salidas / planes",         100000, "Rappicard"),
            ("🚌 Colchón transporte",        80000, "Efectivo / Rappicard"),
            ("🐷 Reserva de la semana",      75000, "Guardar"),
        ],
        "tip": "Semana estándar. La reserva se acumula para el cierre 🏁",
    },
    {
        "label": "Semana 4",
        "fechas": "18 Jul – 24 Jul",
        "presupuesto": 415000,
        "items": [
            ("🛵 Domicilios / delivery",   160000, "Rappicard"),
            ("☕ Salidas / planes",         100000, "Rappicard"),
            ("🚌 Colchón transporte",        80000, "Efectivo / Rappicard"),
            ("🐷 Reserva de la semana",      75000, "Guardar"),
        ],
        "tip": "Recta final. No arranques caprichos nuevos esta semana 🙏",
    },
    {
        "label": "Cierre del ciclo",
        "fechas": "25 Jul – 28 Jul",
        "presupuesto": 180000,
        "items": [
            ("🛵 Domicilios básicos",        80000, "Rappicard"),
            ("☕ Salidas mínimas",            40000, "Rappicard"),
            ("🚌 Transporte",                30000, "Efectivo"),
            ("🐷 Colchón final",             30000, "Guardar"),
        ],
        "tip": "Solo 4 días. Modo conservador: llega limpia al pago del 28 💚",
    },
]

def semana_actual() -> int:
    """Devuelve el índice (0-4) de la semana actual según la fecha."""
    hoy = date.today()
    limites = [
        date(2026, 7, 3),
        date(2026, 7, 10),
        date(2026, 7, 17),
        date(2026, 7, 24),
        date(2026, 7, 28),
    ]
    for i, limite in enumerate(limites):
        if hoy <= limite:
            return i
    return 4

def construir_mensaje(idx: int) -> str:
    s = SEMANAS[idx]
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
        "🃏 *Recuerda:* Rappicard primero (1% cashback). Efectivo solo donde no hay datáfono.",
        f"\n_Ciclo: 27 Jun → 28 Jul · ${1840000:,} total_".replace(",", "."),
    ]
    return "\n".join(lineas)

async def enviar_mensaje(texto: str) -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{BASE_URL}/sendMessage",
            json={
                "chat_id": CHAT_ID,
                "text": texto,
                "parse_mode": "Markdown",
            },
            timeout=10,
        )
        return resp.json()

def construir_resumen_ciclo() -> str:
    total = sum(s["presupuesto"] for s in SEMANAS)
    lineas = [
        "📊 *Resumen del ciclo — 27 Jun → 28 Jul*",
        "━━━━━━━━━━━━━━━━━━",
    ]
    for s in SEMANAS:
        lineas.append(f"📅 *{s['label']}* ({s['fechas']})\n   💰 `${s['presupuesto']:,}`".replace(",", "."))
    lineas += [
        "━━━━━━━━━━━━━━━━━━",
        f"💵 *Total del ciclo: ${total:,}*".replace(",", "."),
        "",
        "🎉 ¡Nuevo ciclo que empieza hoy! Plata lista, cabeza fría.",
        "_Rappicard primero · Reservas al lado · Sin caprichos de más_ 💚",
    ]
    return "\n".join(lineas)

async def main():
    resumen = "--resumen" in sys.argv
    if resumen:
        mensaje = construir_resumen_ciclo()
        label = "Resumen del ciclo"
    else:
        idx = semana_actual()
        mensaje = construir_mensaje(idx)
        label = SEMANAS[idx]["label"]

    resultado = await enviar_mensaje(mensaje)
    if resultado.get("ok"):
        print(f"✅ Mensaje enviado correctamente ({label})")
    else:
        print(f"❌ Error: {resultado}")

if __name__ == "__main__":
    asyncio.run(main())