import asyncio
import json
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Optional
import os
import sys
import httpx
import holidays
from dotenv import load_dotenv
from sheets import registrar_gasto, leer_gastos, borrar_ultimo_gasto

load_dotenv()

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
BASE_URL = f"https://api.telegram.org/bot{TOKEN}"
STATE_FILE = "state.json"
ZONA_COLOMBIA = ZoneInfo("America/Bogota")

def hoy_colombia() -> date:
    """Fecha actual en Colombia. El runner de GitHub Actions corre en UTC,
    así que date.today() adelanta el día 5 horas antes de tiempo."""
    return datetime.now(ZONA_COLOMBIA).date()

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

def calcular_semanas(
    remanente: int,
    fecha_pago: date,
    n_semanas: int = 4,
    offset: int = 0,
    fin_ciclo: Optional[date] = None,
) -> list[dict]:
    """n_semanas/offset permiten recalcular solo una parte del ciclo (ej. el
    ajuste de mitad de ciclo en Semana 3, que solo rehace 2 semanas + cierre).
    Los pesos (22.5% semana, 10% cierre) son relativos entre sí, así que al
    reducir n_semanas siguen sumando 100% de lo que quede por repartir."""
    total = (remanente // 1000) * 1000

    PESO_SEMANA = 22.5
    PESO_CIERRE = 10
    peso_total = n_semanas * PESO_SEMANA + PESO_CIERRE

    p_semana = round(total * PESO_SEMANA / peso_total / 1000) * 1000
    p_cierre = total - n_semanas * p_semana

    tips = [
        "Arranque limpio del ciclo. ¡Cabeza fría! 💪",
        "Semana del capricho 🎉 Solo UN capricho.",
        "Semana estándar. La reserva se acumula para el cierre 🏁",
        "Recta final. No arranques caprichos nuevos 🙏",
    ]

    semanas = []
    inicio = fecha_pago

    for i in range(n_semanas):
        idx_global = offset + i
        fin = inicio + timedelta(days=6)
        es_capricho = (idx_global == 1)

        d = round(p_semana * 0.386 / 1000) * 1000
        t = round(p_semana * 0.193 / 1000) * 1000
        s = round(p_semana * 0.241 / 1000) * 1000
        r = p_semana - d - t - s  # el resto evita errores de redondeo

        semanas.append({
            "label": f"Semana {idx_global + 1}",
            "fechas": f"{_fmt(inicio)} – {_fmt(fin)}",
            "presupuesto": p_semana,
            "presupuesto_tarjeta": d + s,
            "presupuesto_efectivo": t,
            "items": [
                ("🛵 Domicilios / delivery", d, "Rappicard"),
                ("☕ Salidas / planes",       s, "Rappicard"),
                ("🚌 Colchón transporte",     t, "Efectivo"),
                ("✨ Capricho" if es_capricho else "🐷 Reserva de la semana",
                 r,
                 "Rappicard o efectivo" if es_capricho else "Guardar"),
            ],
            "tip": tips[min(idx_global, len(tips) - 1)],
            "fecha_inicio": str(inicio),
            "fecha_fin": str(fin),
        })
        inicio = fin + timedelta(days=1)

    # Fecha fin del ciclo = día de nómina del mes siguiente (salvo que se pase explícita)
    if fin_ciclo is None:
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
        "presupuesto_tarjeta": d_c + s_c,
        "presupuesto_efectivo": t_c,
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

def recalcular_medio_ciclo(
    tarjeta_real: int,
    efectivo_real: int,
    fecha_inicio: date,
    fin_ciclo: date,
    n_semanas: int = 2,
    label_inicio: int = 3,
) -> list[dict]:
    """Reparte la plata REAL que queda en tarjeta y efectivo entre las
    `n_semanas` semanas que faltan (numeradas desde `label_inicio`) y el
    Cierre, usando los mismos pesos relativos del ciclo original (22.5% por
    semana, 10% el cierre). No reintroduce las subcategorías
    (domicilios/salidas/transporte) porque esa plata ya no es un remanente
    fresco: es lo que el usuario reporta que le queda hoy.
    n_semanas/label_inicio generalizan el ajuste para poder dispararlo desde
    cualquier semana futura (no solo el checkpoint automático de la Semana 3)."""
    PESO_SEMANA = 22.5
    PESO_CIERRE = 10
    peso_total = n_semanas * PESO_SEMANA + PESO_CIERRE

    def _reparte(monto: int) -> tuple:
        por_semana = round(monto * PESO_SEMANA / peso_total / 1000) * 1000
        cierre = monto - n_semanas * por_semana
        return por_semana, cierre

    tarjeta_semana, tarjeta_cierre = _reparte(tarjeta_real)
    efectivo_semana, efectivo_cierre = _reparte(efectivo_real)

    def _semana(label, tip, tarjeta, efectivo, inicio, fin):
        return {
            "label": label,
            "fechas": f"{_fmt(inicio)} – {_fmt(fin)}",
            "presupuesto": tarjeta + efectivo,
            "presupuesto_tarjeta": tarjeta,
            "presupuesto_efectivo": efectivo,
            "items": [
                ("💳 Tarjeta (Rappicard)", tarjeta, "Rappicard"),
                ("💵 Efectivo", efectivo, "Efectivo"),
            ],
            "tip": tip,
            "fecha_inicio": str(inicio),
            "fecha_fin": str(fin),
            "gastado": 0,
            "saldo": tarjeta + efectivo,
            "gastado_tarjeta": 0,
            "gastado_efectivo": 0,
            "saldo_tarjeta": tarjeta,
            "saldo_efectivo": efectivo,
        }

    resultado = []
    inicio = fecha_inicio
    for i in range(n_semanas):
        fin = inicio + timedelta(days=6)
        if i == 0:
            tip = "Ajuste real a mitad de ciclo 🎯"
        elif i == n_semanas - 1:
            tip = "Recta final con plata real 🙏"
        else:
            tip = "Sigue con cabeza fría, vas por buen camino 💪"
        resultado.append(_semana(f"Semana {label_inicio + i}", tip, tarjeta_semana, efectivo_semana, inicio, fin))
        inicio = fin + timedelta(days=1)

    cierre = _semana("Cierre del ciclo", "Modo conservador: llega limpia al próximo pago 💚", tarjeta_cierre, efectivo_cierre, inicio, fin_ciclo)
    resultado.append(cierre)

    return resultado

# ── Mensajes ──────────────────────────────────────────────────────────────────

def construir_mensaje_semana(s: dict, total_ciclo: int, fecha_pago: date) -> str:
    mes_sig = fecha_pago.month % 12 + 1
    año_sig = fecha_pago.year + (1 if fecha_pago.month == 12 else 0)
    fin_ciclo = dia_nomina(mes_sig, año_sig)

    lineas = [
        f"💚 *Presupuesto semanal — {s['label']}*",
        f"📅 {s['fechas']}",
        f"💰 Total disponible: *${s['presupuesto']:,}*".replace(",", "."),
    ]
    if "presupuesto_tarjeta" in s:
        lineas.append(
            f"💳 Tarjeta: ${s['presupuesto_tarjeta']:,} · 💵 Efectivo: ${s['presupuesto_efectivo']:,}\n"
            .replace(",", ".")
        )
    lineas.append("━━━━━━━━━━━━━━━━━━")
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

# ── Reporte de cierre de ciclo ────────────────────────────────────────────────
# Cruza lo presupuestado (state.json) contra lo real (Google Sheets) del ciclo
# que termina: por categoría, semana con más gasto y cashback Rappicard (1%).

def _categoria_de_item(nombre: str) -> str:
    n = nombre.lower()
    if "domicilio" in n:
        return "domicilios"
    if "salida" in n:
        return "salidas"
    if "transporte" in n:
        return "transporte"
    if "capricho" in n:
        return "capricho"
    return "reserva"

def construir_reporte_cierre(estado: dict, gastos: list) -> str:
    semanas = estado["semanas"]
    inicio = estado["fecha_nomina"]

    # Solo gastos del ciclo que termina (fechas ISO comparan bien como texto)
    ciclo = []
    for g in gastos:
        fecha = str(g.get("fecha", ""))
        monto = parsear_monto(str(g.get("monto", "")))
        if fecha >= inicio and monto:
            ciclo.append({
                "monto": monto,
                "categoria": str(g.get("categoria", "")).strip().lower(),
                "medio": str(g.get("medio", "")).strip().lower(),
            })

    presupuestado = {}
    for s in semanas:
        for nombre, valor, _ in s["items"]:
            cat = _categoria_de_item(nombre)
            presupuestado[cat] = presupuestado.get(cat, 0) + valor

    real = {}
    for g in ciclo:
        real[g["categoria"]] = real.get(g["categoria"], 0) + g["monto"]

    lineas = [
        "📊 *Cierre del ciclo que termina*",
        "━━━━━━━━━━━━━━━━━━",
        "*Presupuestado vs real:*",
    ]
    for cat in sorted(set(presupuestado) | set(real)):
        p = presupuestado.get(cat, 0)
        r = real.get(cat, 0)
        marca = "✅" if r <= p else "🔴"
        lineas.append(f"{marca} {cat}: `${r:,}` de `${p:,}`".replace(",", "."))

    total_p = sum(s["presupuesto"] for s in semanas)
    total_r = sum(g["monto"] for g in ciclo)
    semana_top = max(semanas, key=lambda s: s.get("gastado", 0))
    cashback = round(sum(g["monto"] for g in ciclo if g["medio"] == "rappicard") * 0.01)

    lineas += [
        "━━━━━━━━━━━━━━━━━━",
        f"💵 Total: *${total_r:,}* de *${total_p:,}*".replace(",", "."),
        f"📈 Semana con más gasto: *{semana_top['label']}* (${semana_top.get('gastado', 0):,})".replace(",", "."),
        f"🃏 Cashback Rappicard: *~${cashback:,}*".replace(",", "."),
    ]
    return "\n".join(lineas)

# ── Detección de patrones ─────────────────────────────────────────────────────
# Analiza TODO el historial de Sheets (no solo el ciclo actual). Cada gasto se
# asigna a su ciclo reconstruyendo el día de nómina del mes correspondiente.
# Solo genera insights con 2+ ciclos de datos: antes no hay patrón comparable.

DIAS_SEMANA = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]

# Filas anteriores a esta fecha son pruebas del desarrollo, no gastos reales
FECHA_INICIO_DATOS = date(2026, 7, 27)

def _ciclo_de(fecha: date) -> date:
    """Día de nómina que inicia el ciclo al que pertenece esta fecha."""
    nomina = dia_nomina(fecha.month, fecha.year)
    if fecha >= nomina:
        return nomina
    mes_ant = fecha.month - 1 if fecha.month > 1 else 12
    año_ant = fecha.year if fecha.month > 1 else fecha.year - 1
    return dia_nomina(mes_ant, año_ant)

def construir_insights(gastos: list) -> Optional[str]:
    registros = []
    for g in gastos:
        try:
            fecha = date.fromisoformat(str(g.get("fecha", "")))
        except ValueError:
            continue
        if fecha < FECHA_INICIO_DATOS:
            continue
        monto = parsear_monto(str(g.get("monto", "")))
        if not monto:
            continue
        ciclo = _ciclo_de(fecha)
        registros.append({
            "monto": monto,
            "categoria": str(g.get("categoria", "")).strip().lower(),
            "medio": str(g.get("medio", "")).strip().lower(),
            "ciclo": str(ciclo),
            "semana": min((fecha - ciclo).days // 7 + 1, 5),
            "dia": fecha.weekday(),
        })

    ciclos = sorted({r["ciclo"] for r in registros})
    if len(ciclos) < 2:
        return None

    total = sum(r["monto"] for r in registros)

    por_cat = {}
    por_semana = {}
    por_dia = {}
    for r in registros:
        por_cat[r["categoria"]] = por_cat.get(r["categoria"], 0) + r["monto"]
        por_semana[r["semana"]] = por_semana.get(r["semana"], 0) + r["monto"]
        por_dia[r["dia"]] = por_dia.get(r["dia"], 0) + r["monto"]

    top_cat, top_cat_monto = max(por_cat.items(), key=lambda kv: kv[1])
    semana_cara, semana_monto = max(por_semana.items(), key=lambda kv: kv[1])
    dia_caro, dia_monto = max(por_dia.items(), key=lambda kv: kv[1])
    promedio_ciclo = round(total / len(ciclos))
    cashback = round(sum(r["monto"] for r in registros if r["medio"] == "rappicard") * 0.01)

    lineas = [
        f"🔍 *Patrones de tus últimos {len(ciclos)} ciclos*",
        "━━━━━━━━━━━━━━━━━━",
        f"🥇 Categoría dominante: *{top_cat}* "
        f"(`${top_cat_monto:,}` · {round(top_cat_monto / total * 100)}% del total)".replace(",", "."),
        f"📆 Semana más cara del ciclo: *Semana {semana_cara}* (`${semana_monto:,}`)".replace(",", "."),
        f"🗓️ Día que más gastas: *{DIAS_SEMANA[dia_caro]}* (`${dia_monto:,}`)".replace(",", "."),
        f"💸 Promedio por ciclo: *${promedio_ciclo:,}*".replace(",", "."),
        f"🃏 Cashback histórico Rappicard: *~${cashback:,}*".replace(",", "."),
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

def parsear_dos_montos(texto: str) -> Optional[tuple]:
    """Extrae dos montos de un mensaje tipo 'tarjeta efectivo'.
    Acepta separarlos por espacio, coma, '/' o ' y '. Ej: '400000 120000'."""
    partes = re.split(r"[,/]| y ", texto.strip(), flags=re.IGNORECASE)
    if len(partes) != 2:
        partes = texto.split()
    if len(partes) != 2:
        return None
    tarjeta, efectivo = parsear_monto(partes[0]), parsear_monto(partes[1])
    if tarjeta is None or efectivo is None:
        return None
    return (tarjeta, efectivo)

# ── Flujo de nómina ───────────────────────────────────────────────────────────

async def procesar_remanente(estado: dict, texto: str, fecha_pago: date) -> bool:
    """Intenta interpretar `texto` como el remanente del mes y arma el ciclo.
    fecha_pago es el día real de nómina (no necesariamente hoy: una respuesta
    puede llegar días después, y las semanas deben arrancar en la fecha de pago).
    Devuelve True si se configuró el ciclo, False si el monto no era válido."""
    remanente = parsear_monto(texto)

    if remanente is None or remanente < 10000:
        await enviar_mensaje(
            "No entendí el monto 😅\n"
            "Escríbelo como número: ej. *1500000* o *1.500.000*"
        )
        return False

    # Monto válido: calcular y enviar el plan del ciclo
    semanas = calcular_semanas(remanente, fecha_pago)
    for s in semanas:
        s["gastado"] = 0
        s["saldo"] = s["presupuesto"]
        s["gastado_tarjeta"] = 0
        s["gastado_efectivo"] = 0
        s["saldo_tarjeta"] = s["presupuesto_tarjeta"]
        s["saldo_efectivo"] = s["presupuesto_efectivo"]
    total = sum(s["presupuesto"] for s in semanas)

    await enviar_mensaje(
        f"✅ Remanente registrado: *${remanente:,}*\n"
        f"Te mando el plan del ciclo ahora 👇".replace(",", ".")
    )

    for semana in semanas:
        await enviar_mensaje(construir_mensaje_semana(semana, total, fecha_pago))

    estado["estado"] = "listo"
    estado["remanente"] = remanente
    estado["semanas"] = semanas
    guardar_estado(estado)
    print(f"✅ Ciclo configurado con remanente ${remanente:,}")
    return True


async def procesar_ajuste_medios(
    estado: dict,
    texto: str,
    fecha_inicio: date,
    fin_ciclo: date,
    idx_inicio: int = 2,
    n_semanas: int = 2,
) -> bool:
    """Ajuste de mitad de ciclo: el usuario reporta cuánto tiene REALMENTE en
    tarjeta y efectivo, y se rehacen las `n_semanas` semanas que faltan desde
    `idx_inicio` (índice 0-based en estado["semanas"]) más el Cierre, a partir
    de esa plata real. idx_inicio/n_semanas por defecto (2/2) preservan el
    checkpoint automático de la Semana 3; flujo_ajuste_manual() los calcula
    dinámicamente para poder dispararse desde cualquier semana futura.
    Devuelve True si se procesó, False si el mensaje no traía los dos montos."""
    montos = parsear_dos_montos(texto)

    if montos is None:
        await enviar_mensaje(
            "No entendí 😅\n"
            "Escribe primero lo que te queda en *tarjeta* y luego en *efectivo*, separados por espacio.\n"
            "_Ej: 400000 120000_"
        )
        return False

    tarjeta_real, efectivo_real = montos
    nuevas = recalcular_medio_ciclo(
        tarjeta_real, efectivo_real, fecha_inicio, fin_ciclo,
        n_semanas=n_semanas, label_inicio=idx_inicio + 1,
    )
    nuevas[0]["ajustada"] = True  # evita que flujo_semanal pregunte remanente redundante en la transición
    if nuevas[0]["fecha_inicio"] == str(hoy_colombia()):
        nuevas[0]["anunciada"] = True  # evita que flujo_semanal repita el aviso hoy mismo
    total_restante = tarjeta_real + efectivo_real
    fecha_pago_original = date.fromisoformat(estado["fecha_nomina"])

    estado["semanas"] = estado["semanas"][:idx_inicio] + nuevas
    estado["estado"] = "listo"
    estado.pop("ajuste_fecha_inicio", None)
    estado.pop("ajuste_fin_ciclo", None)
    estado.pop("ajuste_idx_inicio", None)
    estado.pop("ajuste_n_semanas", None)
    guardar_estado(estado)

    await enviar_mensaje(
        f"✅ Ajuste registrado: 💳 *${tarjeta_real:,}* · 💵 *${efectivo_real:,}*\n"
        f"Te mando el nuevo reparto para lo que falta del ciclo 👇".replace(",", ".")
    )
    for semana in nuevas:
        await enviar_mensaje(construir_mensaje_semana(semana, total_restante, fecha_pago_original))

    print(f"✅ Ajuste medio-ciclo: tarjeta ${tarjeta_real:,}, efectivo ${efectivo_real:,}")
    return True


async def flujo_nomina(hoy: date) -> None:
    estado = leer_estado()

    # Si ya configuramos el ciclo para esta nómina, no repetir
    if estado.get("estado") == "listo" and estado.get("fecha_nomina") == str(hoy):
        print("✅ Ciclo ya configurado para esta nómina. Sin acción.")
        return

    # Reporte de cierre del ciclo anterior (si existió)
    if estado.get("semanas"):
        try:
            gastos = leer_gastos()
            await enviar_mensaje(construir_reporte_cierre(estado, gastos))
            insights = construir_insights(gastos)
            if insights:
                await enviar_mensaje(insights)
        except Exception as e:
            print(f"⚠️ No se pudo generar el reporte de cierre: {e}")

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
                if await procesar_remanente(estado, texto, hoy):
                    return

    print("⏰ Timeout: el usuario no respondió en 10 minutos.")


# ── Flujo de gastos ──────────────────────────────────────────────────────────────────

def es_reporte_gasto(texto:str) -> bool:
    """ Detecta si el mensaje es un reporte de gasto y devuelve True/False"""
    return bool(re.search(r"hoy gasté|hoy pedí|hoy compré|hoy gaste|hoy pedi|hoy compre", texto, re.IGNORECASE))

def es_borrar_gasto(texto: str) -> bool:
    """Detecta si el mensaje pide borrar el último gasto registrado."""
    return bool(re.search(r"borrar( el)?( último| ultimo)? gasto|elimina(r)?( el)? gasto|cancela(r)?( el)? gasto", texto, re.IGNORECASE))

async def deshacer_ultimo_gasto(estado: dict) -> Optional[str]:
    """Borra la última fila de Sheets y revierte su impacto en la semana donde
    se registró (gastado/saldo, y su parte de tarjeta o efectivo). Devuelve el
    mensaje de confirmación, o None si no había ningún gasto que borrar."""
    fila = borrar_ultimo_gasto()
    if fila is None:
        return None

    monto = parsear_monto(str(fila.get("monto", "")))
    categoria = str(fila.get("categoria", "")).strip().lower()
    medio = str(fila.get("medio", "")).strip().lower()
    nota = str(fila.get("nota", "")).strip()
    fecha = str(fila.get("fecha", ""))

    if monto:
        for s in estado.get("semanas", []):
            if s.get("fecha_inicio", "") <= fecha <= s.get("fecha_fin", ""):
                s["gastado"] = max(0, s.get("gastado", 0) - monto)
                s["saldo"] = s["presupuesto"] - s["gastado"]
                if medio == "rappicard":
                    s["gastado_tarjeta"] = max(0, s.get("gastado_tarjeta", 0) - monto)
                elif medio == "efectivo":
                    s["gastado_efectivo"] = max(0, s.get("gastado_efectivo", 0) - monto)
                s["saldo_tarjeta"] = s.get("presupuesto_tarjeta", 0) - s.get("gastado_tarjeta", 0)
                s["saldo_efectivo"] = s.get("presupuesto_efectivo", 0) - s.get("gastado_efectivo", 0)
                break

    mensaje = f"🗑️ Borrado: `${monto or 0:,}` · {categoria} · {medio}".replace(",", ".")
    if nota:
        mensaje += f" · _{nota}_"
    mensaje += "\n\nEscribe el gasto correcto cuando quieras."
    return mensaje


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

    registrar_gasto(str(hoy_colombia()), monto, categoria, medio)
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
    hoy = str(hoy_colombia())
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
    # Nota libre tras un guion (" - "): se separa antes de parsear monto/categoría/medio
    # para que números o palabras dentro de la nota no interfieran (ej. "- cumpleaños el 25").
    partes = re.split(r"\s+-\s+", texto, maxsplit=1)
    cuerpo = partes[0]
    nota = partes[1].strip() if len(partes) > 1 and partes[1].strip() else None

    monto = parsear_monto(cuerpo)

    # Palabras completas del mensaje: evita que "rappicard" active el sinónimo "rappi"
    palabras = set(re.findall(r"[a-záéíóúñü]+", cuerpo.lower()))

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

    return (monto, categoria, medio, nota)
 

# ── Flujo semanal ─────────────────────────────────────────────────────────────
# Corre todos los días: solo actúa si hoy es fecha_inicio de una semana del ciclo.
# La Semana 1 se excluye porque flujo_nomina ya envía el plan completo ese día.

async def flujo_semanal() -> None:
    estado = leer_estado()
    if estado.get("estado") != "listo":
        print("⏭️  Ciclo no inicializado. Sin acción.")
        return

    hoy = str(hoy_colombia())
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

    # Evita doble ejecución (ej. cron atrasado + disparo manual el mismo día):
    # sin esto, una segunda ejecución vuelve a preguntar el remanente y esperar_respuesta
    # puede terminar leyendo un mensaje de gasto pendiente como si fuera la respuesta.
    if semana.get("anunciada"):
        print(f"⏭️  {semana['label']} ya fue anunciada hoy. Sin acción.")
        return
    semana["anunciada"] = True
    guardar_estado(estado)

    fecha_pago = date.fromisoformat(estado["fecha_nomina"])
    total = sum(s["presupuesto"] for s in semanas)

    # Ajuste de mitad de ciclo: en Semana 3, en vez de la pregunta genérica de
    # remanente, se pide la plata REAL en tarjeta y efectivo y se rehacen
    # Semana 3, Semana 4 y el Cierre a partir de eso (ver procesar_ajuste_medios).
    if idx == 2:
        fin_ciclo = date.fromisoformat(semanas[-1]["fecha_fin"])
        await enviar_mensaje(
            "🔄 *¡Arrancamos la Semana 3!* Antes del presupuesto, hagamos un ajuste con plata real.\n\n"
            "¿Cuánto tienes disponible *ahora mismo* en tarjeta (Rappicard) y en efectivo?\n"
            "Escribe los dos montos separados por espacio: primero tarjeta, luego efectivo.\n"
            "_Ej: 400000 120000_"
        )
        estado["estado"] = "esperando_ajuste_medios"
        estado["ajuste_fecha_inicio"] = semana["fecha_inicio"]
        estado["ajuste_fin_ciclo"] = str(fin_ciclo)
        estado["ajuste_idx_inicio"] = idx
        estado["ajuste_n_semanas"] = len(semanas) - 1 - idx
        guardar_estado(estado)
        print("⏳ Esperando ajuste de medios (tarjeta/efectivo) para Semana 3.")
        return

    anterior = semanas[idx - 1]
    await enviar_mensaje(construir_mensaje_semana(semana, total, fecha_pago))

    # Si esta semana ya nació de un ajuste manual/automático reciente (ver
    # procesar_ajuste_medios), el remanente que dejó "anterior" ya quedó
    # incluido en la plata real reportada en ese ajuste — preguntarlo de
    # nuevo lo contaría doble.
    if semana.get("ajustada"):
        guardar_estado(estado)
        print(f"⏭️  {semana['label']} viene de un ajuste reciente; no se pregunta remanente.")
        return

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

# ── Ajuste manual (bajo demanda) ───────────────────────────────────────────────
# A diferencia del checkpoint automático de la Semana 3 (ver flujo_semanal),
# esto se dispara a mano (workflow_dispatch modo=ajuste) cualquier día del
# ciclo. Recalcula desde la próxima semana que aún no ha empezado (la semana
# en curso, si la hay, se deja tal cual porque ya está en marcha) más el
# Cierre, con la plata real que el usuario reporte en ese momento.

async def flujo_ajuste_manual() -> None:
    estado = leer_estado()
    if estado.get("estado") != "listo" or not estado.get("semanas"):
        print("⏭️  No hay ciclo configurado. Sin acción.")
        return

    hoy = str(hoy_colombia())
    semanas = estado["semanas"]

    idx = None
    for i, s in enumerate(semanas):
        if s["label"] != "Cierre del ciclo" and s["fecha_inicio"] > hoy:
            idx = i
            break

    if idx is None:
        await enviar_mensaje("No quedan semanas futuras en este ciclo para ajustar 🤷")
        print("⏭️  No hay semanas futuras que ajustar.")
        return

    fin_ciclo = date.fromisoformat(semanas[-1]["fecha_fin"])
    fecha_inicio = date.fromisoformat(semanas[idx]["fecha_inicio"])
    n_semanas = len(semanas) - 1 - idx  # todas las "Semana N" que quedan, sin contar el Cierre

    await enviar_mensaje(
        "🔄 *Ajuste manual del ciclo*\n\n"
        "¿Cuánto tienes disponible *ahora mismo* en tarjeta (Rappicard) y en efectivo?\n"
        "Escribe los dos montos separados por espacio: primero tarjeta, luego efectivo.\n"
        "_Ej: 400000 120000_"
    )
    estado["estado"] = "esperando_ajuste_medios"
    estado["ajuste_fecha_inicio"] = str(fecha_inicio)
    estado["ajuste_fin_ciclo"] = str(fin_ciclo)
    estado["ajuste_idx_inicio"] = idx
    estado["ajuste_n_semanas"] = n_semanas
    guardar_estado(estado)
    print(f"⏳ Esperando ajuste manual (tarjeta/efectivo) desde {semanas[idx]['label']}...")

    respuesta = await esperar_respuesta(estado, timeout=600)
    if respuesta is None:
        print("⏰ Timeout: sin respuesta al ajuste manual.")
        return

    await procesar_ajuste_medios(estado, respuesta, fecha_inicio, fin_ciclo, idx, n_semanas)

# ── Escucha de gastos ─────────────────────────────────────────────────────────

async def flujo_escucha() -> None:
    estado = leer_estado()

    # Respuesta de remanente pendiente (ej. flujo_nomina expiró sin recibirla):
    # sin esto, un "listo" tardío se queda esperando para siempre porque ningún
    # otro flujo revisa mensajes mientras el estado no sea "listo".
    if estado.get("estado") == "esperando_remanente":
        offset = estado.get("ultimo_update_id", 0) + 1
        fecha_pago = date.fromisoformat(estado["fecha_nomina"])
        async with httpx.AsyncClient() as client:
            updates = await obtener_updates(client, offset)
            for update in updates:
                estado["ultimo_update_id"] = update["update_id"]

                msg = update.get("message", {})
                if str(msg.get("chat", {}).get("id")) != str(CHAT_ID):
                    continue

                texto = msg.get("text", "")
                if await procesar_remanente(estado, texto, fecha_pago):
                    return
        guardar_estado(estado)
        return

    # Ajuste de mitad de ciclo pendiente (Semana 3 o ajuste manual): mismo
    # patrón que arriba, para que una respuesta tardía sobre tarjeta/efectivo
    # no se pierda. idx_inicio/n_semanas quedan guardados en estado porque el
    # ajuste puede venir del checkpoint automático o de flujo_ajuste_manual().
    if estado.get("estado") == "esperando_ajuste_medios":
        offset = estado.get("ultimo_update_id", 0) + 1
        fecha_inicio = date.fromisoformat(estado["ajuste_fecha_inicio"])
        fin_ciclo = date.fromisoformat(estado["ajuste_fin_ciclo"])
        idx_inicio = estado.get("ajuste_idx_inicio", 2)
        n_semanas = estado.get("ajuste_n_semanas", 2)
        async with httpx.AsyncClient() as client:
            updates = await obtener_updates(client, offset)
            for update in updates:
                estado["ultimo_update_id"] = update["update_id"]

                msg = update.get("message", {})
                if str(msg.get("chat", {}).get("id")) != str(CHAT_ID):
                    continue

                texto = msg.get("text", "")
                if await procesar_ajuste_medios(estado, texto, fecha_inicio, fin_ciclo, idx_inicio, n_semanas):
                    return
        guardar_estado(estado)
        return

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

            if es_borrar_gasto(texto):
                resultado = await deshacer_ultimo_gasto(estado)
                await enviar_mensaje(resultado or "No hay gastos para borrar 🤷")
                continue

            if not es_reporte_gasto(texto):
                continue

            monto, categoria, medio, nota = parsear_gasto_texto(texto)
            if not monto or not categoria or not medio:
                await enviar_mensaje(
                    f"No entendí ese gasto 😅\n"
                    f"Formato: _hoy gasté 25000 salidas rappicard_\n"
                    f"Con nota (opcional): _hoy gasté 25000 salidas rappicard - cumpleaños de Ana_"
                )
                continue

            registrar_gasto(str(hoy_colombia()), monto, categoria, medio, nota or "")

            semana = semana_en_curso(estado)
            total_rappicard_ciclo = None
            if semana:
                semana["gastado"] += monto
                semana["saldo"] = semana["presupuesto"] - semana["gastado"]

                if medio == "rappicard":
                    semana["gastado_tarjeta"] = semana.get("gastado_tarjeta", 0) + monto
                elif medio == "efectivo":
                    semana["gastado_efectivo"] = semana.get("gastado_efectivo", 0) + monto
                semana["saldo_tarjeta"] = semana.get("presupuesto_tarjeta", 0) - semana.get("gastado_tarjeta", 0)
                semana["saldo_efectivo"] = semana.get("presupuesto_efectivo", 0) - semana.get("gastado_efectivo", 0)

                if medio == "rappicard":
                    total_rappicard_ciclo = sum(s.get("gastado_tarjeta", 0) for s in estado.get("semanas", []))

            gastos_registrados.append((monto, categoria, medio, nota, total_rappicard_ciclo))

    guardar_estado(estado)

    if not gastos_registrados:
        print("📭 Sin mensajes de gasto pendientes.")
        return

    lineas = ["📋 *Gastos registrados:*"]
    for monto, categoria, medio, nota, total_rappicard_ciclo in gastos_registrados:
        linea = f"   · `${monto:,}` · {categoria} · {medio}".replace(",", ".")
        if nota:
            linea += f" · _{nota}_"
        lineas.append(linea)
        if total_rappicard_ciclo is not None:
            lineas.append(
                f"      🃏 Ya llevas *${total_rappicard_ciclo:,}* en RappiCard este ciclo".replace(",", ".")
            )

    semana = semana_en_curso(estado)
    if semana:
        reserva = semana["presupuesto"] - semana.get("presupuesto_tarjeta", 0) - semana.get("presupuesto_efectivo", 0)
        lineas.append(f"\n📊 Saldo restante esta semana: *${semana['saldo']:,}*".replace(",", "."))
        lineas.append(
            f"💳 Tarjeta: ${semana.get('saldo_tarjeta', 0):,} · 💵 Efectivo: ${semana.get('saldo_efectivo', 0):,} · 🐷 Reserva: ${reserva:,}"
            .replace(",", ".")
        )
        if semana["saldo"] < 0:
            await enviar_mensaje("\n".join(lineas))
            await flujo_buffer(estado, semana)
            return

    await enviar_mensaje("\n".join(lineas))

    # Alerta proactiva: una sola vez por semana al cruzar el 70% del presupuesto
    if (
        semana
        and not semana.get("alerta_70")
        and semana["presupuesto"] > 0
        and semana["gastado"] >= semana["presupuesto"] * 0.7
    ):
        semana["alerta_70"] = True
        guardar_estado(estado)
        pct = round(semana["gastado"] / semana["presupuesto"] * 100)
        await enviar_mensaje(
            f"🚨 *Ojo:* ya llevas el *{pct}%* del presupuesto de la {semana['label']}.\n"
            f"Te quedan *${semana['saldo']:,}* hasta el {_fmt(date.fromisoformat(semana['fecha_fin']))}. "
            f"Modo cabeza fría 🧊".replace(",", ".")
        )
        print(f"🚨 Alerta 70% enviada ({pct}%)")

    print(f"✅ {len(gastos_registrados)} gasto(s) procesado(s)")

# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    hoy = hoy_colombia()

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

    elif "--insights" in sys.argv:
        mensaje = construir_insights(leer_gastos())
        if mensaje:
            await enviar_mensaje(mensaje)
            print("✅ Insights enviados")
        else:
            print("⏭️  Aún no hay 2 ciclos completos de datos. Sin acción.")

    elif "--gasto" in sys.argv:
        estado = leer_estado()
        await flujo_gastos(estado)

    elif "--ajuste" in sys.argv:
        await flujo_ajuste_manual()

    else:
        await flujo_semanal()

if __name__ == "__main__":
    asyncio.run(main())
