import asyncio
import calendar
import re
import unicodedata
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Optional
import os
import sys
import httpx
from dotenv import load_dotenv
from sheets import registrar_gasto, leer_gastos, borrar_ultimo_gasto
from estado_store import leer_estado, guardar_estado, registrar_movimiento
from clasificador import clasificar_gasto

load_dotenv()

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
BASE_URL = f"https://api.telegram.org/bot{TOKEN}"
ZONA_COLOMBIA = ZoneInfo("America/Bogota")

def hoy_colombia() -> date:
    """Fecha actual en Colombia. El runner de GitHub Actions corre en UTC,
    así que date.today() adelanta el día 5 horas antes de tiempo."""
    return datetime.now(ZONA_COLOMBIA).date()

# ── Estado ────────────────────────────────────────────────────────────────────
# El estado conversacional vive en Firestore (ver estado_store.py), no en un
# archivo local — dos sistemas (GitHub Actions hoy, Cloud Run en la Fase B)
# pueden escribirlo a la vez y state.json en git no soporta eso sin condiciones
# de carrera. Estructura:
#   estado:              "idle" | "esperando_presupuestos" | "esperando_ajuste_medios" | "listo"
#   ultimo_update_id:    el último mensaje de Telegram que ya procesamos
#   fecha_nomina:        fecha del último día de nómina procesado (evita repetir)
#   dia_nomina_config:   día del mes (1-31) configurado como día de pago, literal
#                        (sin ajuste por fin de semana/festivo)
#   fondos:              {"rappicard"|"gastos_personales": {"presupuesto": int, "gastado": int}}
#                        del ciclo actual. Es lo ÚNICO que se presupuesta por semana.
#   bolsillos:           {slug: {"nombre": str, "saldo": int}}. Solo saldos visuales:
#                        no se presupuestan ni afectan el presupuesto semanal.
#                        Sus ingresos/retiros se guardan en Firestore (registrar_movimiento).

# ── Fondos presupuestados y bolsillos ─────────────────────────────────────────
# Presupuesto semanal = Rappicard + Gastos personales (nada más).
# Un gasto se enruta así (ver destino_gasto):
#   1. si su categoría es un bolsillo → solo descuenta de ese bolsillo
#   2. si no, el medio decide: rappicard → cupo Rappicard; cualquier otro → Gastos personales
# La categoría (domicilios, salidas…) es solo una etiqueta para reportes.

FONDOS_LABELS = {
    "rappicard": "💳 Rappicard",
    "gastos_personales": "🎉 Gastos personales",
}

MEDIOS = {
    "rappicard": ["rappicard", "tarjeta", "credito", "crédito"],
    "efectivo":  ["efectivo", "cash", "billete"],
}

def slug(texto: str) -> str:
    """'Seguridad Social' → 'seguridad_social' (sin tildes, para comparar nombres)."""
    sin_tildes = unicodedata.normalize("NFD", texto.strip().lower())
    sin_tildes = "".join(c for c in sin_tildes if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", "_", sin_tildes).strip("_")

def buscar_bolsillo(estado: dict, texto: Optional[str]) -> Optional[str]:
    """Slug del bolsillo cuyo nombre aparece en `texto` (como palabra completa), o None."""
    if not texto:
        return None
    t = f"_{slug(texto)}_"
    exacto = slug(texto)
    if exacto in estado.get("bolsillos", {}):
        return exacto
    # Más largo primero: "seguridad_social" gana sobre un bolsillo "seguridad"
    for clave in sorted(estado.get("bolsillos", {}), key=len, reverse=True):
        if f"_{clave}_" in t:
            return clave
    return None

def destino_gasto(estado: dict, categoria: str, medio: Optional[str]) -> str:
    """Devuelve 'bolsillo:<slug>', 'rappicard' o 'gastos_personales'."""
    clave = buscar_bolsillo(estado, categoria)
    if clave:
        return f"bolsillo:{clave}"
    return "rappicard" if medio == "rappicard" else "gastos_personales"

def formato_cop(n: int) -> str:
    return f"${n:,}".replace(",", ".")

# ── Fechas ────────────────────────────────────────────────────────────────────

DIA_NOMINA_DEFAULT = 17

def dia_nomina(mes: int, año: int, dia: int = DIA_NOMINA_DEFAULT) -> date:
    """Día de nómina configurado por el usuario, literal (sin ajuste por fin de
    semana o festivo). Si el mes no tiene ese día (ej. 31 en febrero), usa el
    último día del mes."""
    ultimo_dia_mes = calendar.monthrange(año, mes)[1]
    return date(año, mes, min(dia, ultimo_dia_mes))

def _fmt(d: date) -> str:
    meses = ["","Ene","Feb","Mar","Abr","May","Jun",
             "Jul","Ago","Sep","Oct","Nov","Dic"]
    return f"{d.day} {meses[d.month]}"

# ── Cálculo dinámico del ciclo ────────────────────────────────────────────────
# Dado un presupuesto por categoría (para el ciclo completo), distribuye cada
# categoría en 4 semanas + cierre con los mismos pesos relativos:
#   - Cada semana normal: ~22.5% del total de cada categoría
#   - Cierre (~4 días):   ~10% del total de cada categoría

PESO_SEMANA = 22.5
PESO_CIERRE = 10

TIPS_SEMANA = [
    "Arranque limpio del ciclo. ¡Cabeza fría! 💪",
    "Sigue con cabeza fría, vas por buen camino 💪",
    "Semana estándar. Cuida el ritmo para llegar bien al cierre 🏁",
    "Recta final. No arranques gastos nuevos que no estén presupuestados 🙏",
]

def calcular_semanas(
    fondos: dict,
    fecha_inicio: date,
    dia_nomina_config: int = DIA_NOMINA_DEFAULT,
    n_semanas: int = 4,
    offset: int = 0,
    fin_ciclo: Optional[date] = None,
) -> list[dict]:
    """Reparte cada fondo ({"rappicard": monto, "gastos_personales": monto}) en
    `n_semanas` semanas + cierre con los pesos relativos de arriba. n_semanas y
    offset permiten recalcular solo una parte del ciclo (ajuste de mitad de
    ciclo): los pesos son relativos entre sí, así que siguen sumando 100% de
    lo que quede por repartir. Cada semana trae, además del total, el
    presupuesto/gastado/saldo de cada fondo para poder descontar por separado."""
    peso_total = n_semanas * PESO_SEMANA + PESO_CIERRE

    por_semana = {}
    por_cierre = {}
    for fondo, monto in fondos.items():
        por_semana[fondo] = round(monto * PESO_SEMANA / peso_total / 1000) * 1000
        por_cierre[fondo] = monto - n_semanas * por_semana[fondo]

    def _semana(label, tip, montos, inicio, fin):
        semana = {
            "label": label,
            "fechas": f"{_fmt(inicio)} – {_fmt(fin)}",
            "presupuesto": sum(montos.values()),
            "items": [(FONDOS_LABELS[f], montos[f], "") for f in fondos],
            "tip": tip,
            "fecha_inicio": str(inicio),
            "fecha_fin": str(fin),
            "gastado": 0,
            "saldo": sum(montos.values()),
        }
        for f in fondos:
            semana[f"presupuesto_{f}"] = montos[f]
            semana[f"gastado_{f}"] = 0
            semana[f"saldo_{f}"] = montos[f]
        return semana

    semanas = []
    inicio = fecha_inicio
    for i in range(n_semanas):
        idx_global = offset + i
        fin = inicio + timedelta(days=6)
        semanas.append(_semana(
            f"Semana {idx_global + 1}", TIPS_SEMANA[min(idx_global, len(TIPS_SEMANA) - 1)],
            por_semana, inicio, fin,
        ))
        inicio = fin + timedelta(days=1)

    # Fecha fin del ciclo = día de nómina del mes siguiente (salvo que se pase explícita)
    if fin_ciclo is None:
        mes_sig = fecha_inicio.month % 12 + 1
        año_sig = fecha_inicio.year + (1 if fecha_inicio.month == 12 else 0)
        fin_ciclo = dia_nomina(mes_sig, año_sig, dia_nomina_config)

    semanas.append(_semana("Cierre del ciclo", "Modo conservador: llega limpia al próximo pago 💚", por_cierre, inicio, fin_ciclo))
    return semanas

def recalcular_medio_ciclo(
    rappicard_real: int,
    personales_real: int,
    fecha_inicio: date,
    fin_ciclo: date,
    n_semanas: int = 2,
    label_inicio: int = 3,
) -> list[dict]:
    """Reparte la plata REAL que el usuario dice tener hoy en Rappicard y en
    Gastos personales entre las `n_semanas` semanas que faltan (numeradas
    desde `label_inicio`) y el Cierre. Es la misma repartición del ciclo
    original, pero partiendo de lo que de verdad queda, no de lo planeado."""
    semanas = calcular_semanas(
        {"rappicard": rappicard_real, "gastos_personales": personales_real},
        fecha_inicio, n_semanas=n_semanas, offset=label_inicio - 1, fin_ciclo=fin_ciclo,
    )
    if n_semanas > 0:
        semanas[0]["tip"] = "Ajuste real a mitad de ciclo 🎯"
    if n_semanas > 1:
        semanas[n_semanas - 1]["tip"] = "Recta final con plata real 🙏"
    return semanas

# ── Mensajes ──────────────────────────────────────────────────────────────────

def construir_mensaje_semana(s: dict, total_ciclo: int, fecha_pago: date, dia_nomina_config: int = DIA_NOMINA_DEFAULT) -> str:
    mes_sig = fecha_pago.month % 12 + 1
    año_sig = fecha_pago.year + (1 if fecha_pago.month == 12 else 0)
    fin_ciclo = dia_nomina(mes_sig, año_sig, dia_nomina_config)

    lineas = [
        f"💚 *Presupuesto semanal — {s['label']}*",
        f"📅 {s['fechas']}",
        f"💰 Total disponible: *${s['presupuesto']:,}*".replace(",", "."),
    ]
    lineas.append("━━━━━━━━━━━━━━━━━━")
    for nombre, valor, medio in s["items"]:
        lineas.append(f"{nombre}: `${valor:,}`".replace(",", "."))
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
# Cruza lo presupuestado (estado["categorias"]) contra lo real (Google Sheets)
# del ciclo que termina: por categoría, semana con más gasto y cashback Rappicard (1%).

def construir_reporte_cierre(estado: dict, gastos: list) -> str:
    semanas = estado["semanas"]
    inicio = estado["fecha_nomina"]
    fondos = estado.get("fondos")  # ausente en ciclos anteriores a la v2 (ver más abajo)

    # Solo gastos del ciclo que termina (fechas ISO comparan bien como texto)
    ciclo = []
    for g in gastos:
        fecha = str(g.get("fecha", ""))
        monto = parsear_monto(str(g.get("monto", "")))
        if fecha >= inicio and monto:
            categoria = str(g.get("categoria", "")).strip().lower()
            medio = str(g.get("medio", "")).strip().lower()
            ciclo.append({
                "monto": monto,
                "categoria": categoria,
                "medio": medio,
                "destino": destino_gasto(estado, categoria, medio) if fondos else categoria,
            })

    lineas = [
        "📊 *Cierre del ciclo que termina*",
        "━━━━━━━━━━━━━━━━━━",
        "*Presupuestado vs real:*",
    ]

    if fondos:
        # v2: lo presupuestado son los fondos; los bolsillos no se presupuestan
        presupuestado = {f: info["presupuesto"] for f, info in fondos.items()}
        real = {}
        for g in ciclo:
            if not g["destino"].startswith("bolsillo:"):
                real[g["destino"]] = real.get(g["destino"], 0) + g["monto"]
        etiquetas = FONDOS_LABELS
    else:
        # ciclo anterior a la v2: presupuesto por categoría
        presupuestado = {cat: info["presupuesto"] for cat, info in estado.get("categorias", {}).items()}
        real = {}
        for g in ciclo:
            real[g["categoria"]] = real.get(g["categoria"], 0) + g["monto"]
        etiquetas = {}

    for clave in sorted(set(presupuestado) | set(real)):
        p = presupuestado.get(clave, 0)
        r = real.get(clave, 0)
        marca = "✅" if r <= p else "🔴"
        lineas.append(f"{marca} {etiquetas.get(clave, clave)}: `{formato_cop(r)}` de `{formato_cop(p)}`")

    if fondos and estado.get("bolsillos"):
        lineas.append("\n*Bolsillos al cierre:*")
        for b in estado["bolsillos"].values():
            lineas.append(f"👛 {b['nombre']}: `{formato_cop(b['saldo'])}`")

    total_p = sum(s["presupuesto"] for s in semanas)
    total_r = sum(real.values())
    semana_top = max(semanas, key=lambda s: s.get("gastado", 0))
    cashback = round(sum(g["monto"] for g in ciclo if g["medio"] == "rappicard") * 0.01)

    lineas += [
        "━━━━━━━━━━━━━━━━━━",
        f"💵 Total: *{formato_cop(total_r)}* de *{formato_cop(total_p)}*",
        f"📈 Semana con más gasto: *{semana_top['label']}* ({formato_cop(semana_top.get('gastado', 0))})",
        f"🃏 Cashback Rappicard: *~{formato_cop(cashback)}*",
    ]
    return "\n".join(lineas)

# ── Detección de patrones ─────────────────────────────────────────────────────
# Analiza TODO el historial de Sheets (no solo el ciclo actual). Cada gasto se
# asigna a su ciclo reconstruyendo el día de nómina del mes correspondiente.
# Solo genera insights con 2+ ciclos de datos: antes no hay patrón comparable.

DIAS_SEMANA = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]

# Filas anteriores a esta fecha son pruebas del desarrollo, no gastos reales
FECHA_INICIO_DATOS = date(2026, 7, 27)

def _ciclo_de(fecha: date, dia_nomina_config: int = DIA_NOMINA_DEFAULT) -> date:
    """Día de nómina que inicia el ciclo al que pertenece esta fecha."""
    nomina = dia_nomina(fecha.month, fecha.year, dia_nomina_config)
    if fecha >= nomina:
        return nomina
    mes_ant = fecha.month - 1 if fecha.month > 1 else 12
    año_ant = fecha.year if fecha.month > 1 else fecha.year - 1
    return dia_nomina(mes_ant, año_ant, dia_nomina_config)

def construir_insights(gastos: list, dia_nomina_config: int = DIA_NOMINA_DEFAULT) -> Optional[str]:
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
        ciclo = _ciclo_de(fecha, dia_nomina_config)
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
    """Extrae dos montos de un mensaje tipo 'rappicard gastos_personales'.
    Acepta separarlos por espacio, coma, '/' o ' y '. Ej: '400000 120000'."""
    montos = parsear_montos_lista(texto, 2)
    return tuple(montos) if montos else None

def parsear_montos_lista(texto: str, n: int) -> Optional[list]:
    """Extrae n montos de un mensaje tipo 'monto1, monto2, ... montoN'.
    Acepta separarlos por coma, '/', ' y ', salto de línea o espacio."""
    partes = re.split(r"[,/\n]| y ", texto.strip(), flags=re.IGNORECASE)
    partes = [p for p in partes if p.strip()]
    if len(partes) != n:
        partes = texto.split()
    if len(partes) != n:
        return None
    montos = [parsear_monto(p) for p in partes]
    if any(m is None for m in montos):
        return None
    return montos

# ── Flujo de nómina ───────────────────────────────────────────────────────────

PALABRAS_RELLENO = {"y", "e", "para", "los", "las", "el", "la", "de", "del", "con", "en", "bolsillo", "bolsillos", "digital", "digitales", "saldo", "saldos", "mis", "tengo"}

def _limpiar_nombre(fragmento: str) -> str:
    """De un fragmento como ' y para los bolsillos digitales\n● mercado' deja 'mercado'."""
    ultima_linea = fragmento.strip().splitlines()[-1] if fragmento.strip() else ""
    palabras = re.findall(r"[a-záéíóúñü]+", ultima_linea.lower())
    while palabras and palabras[0] in PALABRAS_RELLENO:
        palabras.pop(0)
    return " ".join(palabras)

def parsear_nomina(texto: str) -> Optional[tuple]:
    """Interpreta la respuesta de nómina: Rappicard, Gastos personales y los
    saldos de los bolsillos. Acepta tanto líneas ('mercado: 100.000') como todo
    en una frase ('rappicard 1.000.000 y gastos personales 200.000').
    Devuelve (rappicard, gastos_personales, {nombre_bolsillo: saldo}) o None si
    falta alguno de los dos fondos. Los bolsillos son opcionales."""
    rappicard = personales = None
    bolsillos = {}
    anterior_fin = 0
    for m in re.finditer(r"\$?\s*(\d[\d.,]*)", texto):
        nombre = _limpiar_nombre(texto[anterior_fin:m.start()])
        anterior_fin = m.end()
        monto = parsear_monto(m.group(1))
        if not nombre or monto is None:
            continue
        if "rappicard" in nombre:
            rappicard = monto
        elif "personales" in nombre:
            personales = monto
        else:
            bolsillos[nombre.title()] = monto
    if rappicard is None or personales is None:
        return None
    return rappicard, personales, bolsillos

def aplicar_saldos_bolsillos(estado: dict, bolsillos: dict) -> list:
    """Fija el saldo de cada bolsillo indicado (crea los nuevos) y devuelve los
    movimientos (slug, nombre, tipo, monto, nota) que explican el cambio."""
    movimientos = []
    existentes = estado.setdefault("bolsillos", {})
    for nombre, saldo in bolsillos.items():
        clave = slug(nombre)
        previo = existentes.get(clave, {}).get("saldo", 0)
        existentes[clave] = {"nombre": existentes.get(clave, {}).get("nombre", nombre), "saldo": saldo}
        delta = saldo - previo
        if delta:
            movimientos.append((clave, existentes[clave]["nombre"], "ingreso" if delta > 0 else "retiro", abs(delta), "Saldo de nómina"))
    return movimientos

def mensaje_pedir_nomina(estado: dict) -> str:
    bolsillos = [b["nombre"] for b in estado.get("bolsillos", {}).values()]
    ejemplo_bolsillos = bolsillos or ["Mercado", "Transporte"]
    lineas_bolsillos = "\n".join(f"{n}: 100000" for n in ejemplo_bolsillos)
    pedir = "Dame el presupuesto de este mes:\n• *Rappicard* (cupo total)\n• *Gastos personales*\n"
    if bolsillos:
        pedir += f"y el saldo de tus bolsillos ({', '.join(bolsillos)}); si alguno no cambia, déjalo fuera.\n"
    else:
        pedir += "y el saldo de tus bolsillos digitales (uno por línea, ej. mercado, transporte, mascota…).\n"
    return (
        "💰 *¡Hoy es día de nómina!*\n\n"
        f"{pedir}\n"
        f"_Ej:_\n```\nRappicard: 1000000\nGastos personales: 200000\n{lineas_bolsillos}\n```"
    )

async def procesar_presupuestos(estado: dict, texto: str, fecha_pago: date, dia_nomina_config: int) -> bool:
    """Intenta interpretar `texto` como la respuesta de nómina y arma el ciclo.
    fecha_pago es el día real de nómina (no necesariamente hoy: una respuesta
    puede llegar días después, y las semanas deben arrancar en la fecha de pago).
    Devuelve True si se configuró el ciclo, False si faltaban montos."""
    interpretado = parsear_nomina(texto)

    if interpretado is None:
        await enviar_mensaje(
            "No encontré el monto de *Rappicard* y de *Gastos personales* 😅\n" + mensaje_pedir_nomina(estado).split("\n\n", 1)[1]
        )
        return False

    rappicard, personales, bolsillos = interpretado

    # n_semanas se deriva de los días reales hasta el próximo pago (no siempre
    # son 4 semanas completas: si el ciclo arranca tarde, ej. porque se saltó
    # una nómina, puede quedar un ciclo corto de menos de 4 semanas + cierre).
    mes_sig = fecha_pago.month % 12 + 1
    año_sig = fecha_pago.year + (1 if fecha_pago.month == 12 else 0)
    fin_ciclo = dia_nomina(mes_sig, año_sig, dia_nomina_config)
    n_semanas = max(0, (fin_ciclo - fecha_pago).days // 7)
    semanas = calcular_semanas(
        {"rappicard": rappicard, "gastos_personales": personales},
        fecha_pago, dia_nomina_config, n_semanas=n_semanas, fin_ciclo=fin_ciclo,
    )
    total = sum(s["presupuesto"] for s in semanas)

    movimientos = aplicar_saldos_bolsillos(estado, bolsillos)
    for clave, nombre, tipo, monto, nota in movimientos:
        registrar_movimiento(str(fecha_pago), clave, nombre, tipo, monto, nota)

    estado["estado"] = "listo"
    estado["fondos"] = {
        "rappicard": {"presupuesto": rappicard, "gastado": 0},
        "gastos_personales": {"presupuesto": personales, "gastado": 0},
    }
    estado.pop("categorias", None)  # presupuesto por categoría de ciclos anteriores a la v2
    estado["semanas"] = semanas
    guardar_estado(estado)

    await enviar_mensaje(
        f"✅ Presupuesto del ciclo: *{formato_cop(total)}*\n"
        f"💳 Rappicard {formato_cop(rappicard)} + 🎉 Gastos personales {formato_cop(personales)}\n"
        "Aquí está tu presupuesto por semana 👇"
    )
    for semana in semanas:
        await enviar_mensaje(construir_mensaje_semana(semana, total, fecha_pago, dia_nomina_config))
    if estado.get("bolsillos"):
        await enviar_mensaje(construir_bolsillos(estado))

    print(f"✅ Ciclo configurado (total {formato_cop(total)}, {len(bolsillos)} bolsillo(s) actualizados)")
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
    Rappicard y en Gastos personales, y se rehacen las `n_semanas` semanas que faltan desde
    `idx_inicio` (índice 0-based en estado["semanas"]) más el Cierre, a partir
    de esa plata real. idx_inicio/n_semanas por defecto (2/2) preservan el
    checkpoint automático de la Semana 3; flujo_ajuste_manual() los calcula
    dinámicamente para poder dispararse desde cualquier semana futura.
    Devuelve True si se procesó, False si el mensaje no traía los dos montos."""
    montos = parsear_dos_montos(texto)

    if montos is None:
        await enviar_mensaje(
            "No entendí 😅\n"
            "Escribe primero lo que te queda en *Rappicard* y luego en *gastos personales*, separados por espacio.\n"
            "_Ej: 400000 120000_"
        )
        return False

    rappicard_real, personales_real = montos
    nuevas = recalcular_medio_ciclo(
        rappicard_real, personales_real, fecha_inicio, fin_ciclo,
        n_semanas=n_semanas, label_inicio=idx_inicio + 1,
    )
    nuevas[0]["ajustada"] = True  # evita que flujo_semanal pregunte remanente redundante en la transición
    if nuevas[0]["fecha_inicio"] == str(hoy_colombia()):
        nuevas[0]["anunciada"] = True  # evita que flujo_semanal repita el aviso hoy mismo
    total_restante = rappicard_real + personales_real
    fecha_pago_original = date.fromisoformat(estado["fecha_nomina"])
    dia_nomina_config = estado.get("dia_nomina_config", DIA_NOMINA_DEFAULT)

    estado["semanas"] = estado["semanas"][:idx_inicio] + nuevas
    # El saldo del ciclo de cada fondo pasa a ser la plata real reportada
    # (lo ya gastado se conserva para el reporte de cierre).
    fondos = estado.setdefault("fondos", {})
    for fondo, real in (("rappicard", rappicard_real), ("gastos_personales", personales_real)):
        gastado = fondos.get(fondo, {}).get("gastado", 0)
        fondos[fondo] = {"presupuesto": gastado + real, "gastado": gastado}
    estado["estado"] = "listo"
    estado.pop("ajuste_fecha_inicio", None)
    estado.pop("ajuste_fin_ciclo", None)
    estado.pop("ajuste_idx_inicio", None)
    estado.pop("ajuste_n_semanas", None)
    guardar_estado(estado)

    await enviar_mensaje(
        f"✅ Ajuste registrado: 💳 *{formato_cop(rappicard_real)}* · 🎉 *{formato_cop(personales_real)}*\n"
        "Te mando el nuevo reparto para lo que falta del ciclo 👇"
    )
    for semana in nuevas:
        await enviar_mensaje(construir_mensaje_semana(semana, total_restante, fecha_pago_original, dia_nomina_config))

    print(f"✅ Ajuste medio-ciclo: rappicard {formato_cop(rappicard_real)}, gastos personales {formato_cop(personales_real)}")
    return True


async def flujo_nomina(hoy: date, forzar: bool = False) -> None:
    """Dispara la pregunta de nómina y termina: la respuesta la procesa
    procesar_mensaje (webhook o --escucha), sin ventana de espera."""
    estado = leer_estado()
    dia_nomina_config = estado.get("dia_nomina_config", DIA_NOMINA_DEFAULT)

    # Si ya configuramos (o ya preguntamos) esta nómina, no repetir
    if not forzar and estado.get("fecha_nomina") == str(hoy) and estado.get("estado") in ("listo", "esperando_presupuestos"):
        print("✅ Nómina de hoy ya iniciada. Sin acción.")
        return

    # Reporte de cierre del ciclo anterior (si existió)
    if estado.get("semanas"):
        try:
            gastos = leer_gastos()
            await enviar_mensaje(construir_reporte_cierre(estado, gastos))
            insights = construir_insights(gastos, dia_nomina_config)
            if insights:
                await enviar_mensaje(insights)
        except Exception as e:
            print(f"⚠️ No se pudo generar el reporte de cierre: {e}")

    await enviar_mensaje(mensaje_pedir_nomina(estado))
    estado["estado"] = "esperando_presupuestos"
    estado["fecha_nomina"] = str(hoy)
    estado.pop("pendiente", None)
    guardar_estado(estado)
    print("⏳ Pregunta de nómina enviada; la respuesta la procesa el receptor de mensajes.")


# ── Flujo de gastos ──────────────────────────────────────────────────────────────────

def es_reporte_gasto(texto:str) -> bool:
    """ Detecta si el mensaje es un reporte de gasto y devuelve True/False"""
    return bool(re.search(r"hoy gasté|hoy pedí|hoy compré|hoy gaste|hoy pedi|hoy compre", texto, re.IGNORECASE))

def es_borrar_gasto(texto: str) -> bool:
    """Detecta si el mensaje pide borrar el último gasto registrado."""
    return bool(re.search(r"borrar( el)?( último| ultimo)? gasto|elimina(r)?( el)? gasto|cancela(r)?( el)? gasto", texto, re.IGNORECASE))

def es_ajuste_dia_nomina(texto: str) -> bool:
    """Detecta si el mensaje pide cambiar el día de nómina configurado."""
    return bool(re.search(r"ajuste en n[oó]mina|cambiar? (el )?d[ií]a de n[oó]mina|cambia mi d[ií]a de pago", texto, re.IGNORECASE))

def es_nuevo_bolsillo(texto: str) -> bool:
    return bool(re.search(r"^\W*nuevo bolsillo", texto, re.IGNORECASE))

def es_movimiento_bolsillo(texto: str) -> bool:
    """'ingreso a mercado 50000', 'retiro de mascota 20.000'."""
    return bool(re.search(r"^\W*(ingres|retir|sac)[a-zé]*\b", texto, re.IGNORECASE))

def es_consulta_saldos(texto: str) -> bool:
    return bool(re.search(r"^\W*(mis )?(saldos?|bolsillos)\W*$", texto, re.IGNORECASE))

def semana_de_fecha(estado: dict, fecha: str) -> Optional[dict]:
    for s in estado.get("semanas", []):
        if s.get("fecha_inicio", "") <= fecha <= s.get("fecha_fin", ""):
            return s
    return None

def mover_gasto(estado: dict, monto: int, destino: str, fecha: str, signo: int = 1) -> None:
    """Aplica (signo=1) o revierte (signo=-1) el impacto de un gasto. Los saldos
    se ajustan por diferencia (no recalculados como presupuesto - gastado) para
    no perder el buffer que haya tomado la semana."""
    if destino.startswith("bolsillo:"):
        bolsillo = estado.get("bolsillos", {}).get(destino.split(":", 1)[1])
        if bolsillo:
            bolsillo["saldo"] -= signo * monto
        return

    fondo = estado.get("fondos", {}).get(destino)
    if fondo:
        fondo["gastado"] = max(0, fondo["gastado"] + signo * monto)
    semana = semana_de_fecha(estado, fecha)
    if semana:
        semana["gastado"] = max(0, semana.get("gastado", 0) + signo * monto)
        semana["saldo"] -= signo * monto
        if f"presupuesto_{destino}" in semana:
            semana[f"gastado_{destino}"] = max(0, semana.get(f"gastado_{destino}", 0) + signo * monto)
            semana[f"saldo_{destino}"] = semana.get(f"saldo_{destino}", 0) - signo * monto

def etiqueta_destino(destino: str) -> str:
    if destino.startswith("bolsillo:"):
        return "👛 Bolsillo"
    return FONDOS_LABELS[destino]

def saldo_fondo(estado: dict, fondo: str) -> int:
    info = estado["fondos"][fondo]
    return info["presupuesto"] - info["gastado"]

def construir_bolsillos(estado: dict) -> str:
    bolsillos = estado.get("bolsillos", {})
    if not bolsillos:
        return "👛 Aún no tienes bolsillos. Crea uno con: _nuevo bolsillo Mercado 100000_"
    lineas = ["👛 *Bolsillos*", "━━━━━━━━━━━━━━━━━━"]
    for b in bolsillos.values():
        lineas.append(f"{b['nombre']}: `{formato_cop(b['saldo'])}`")
    lineas += ["━━━━━━━━━━━━━━━━━━", f"Total en bolsillos: *{formato_cop(sum(b['saldo'] for b in bolsillos.values()))}*"]
    return "\n".join(lineas)

def construir_saldos(estado: dict) -> str:
    """Rappicard, semana en curso y gastos personales, como en la confirmación
    de un gasto, más los bolsillos."""
    lineas = ["💰 *Tus saldos*", "━━━━━━━━━━━━━━━━━━"]
    if "fondos" in estado:
        lineas.append(f"💳 Rappicard: *{formato_cop(saldo_fondo(estado, 'rappicard'))}*")
    semana = semana_en_curso(estado)
    if semana:
        lineas.append(f"📅 Saldo de la semana ({semana['label']}): *{formato_cop(semana['saldo'])}*")
    if "fondos" in estado:
        lineas.append(f"🎉 Gastos personales: *{formato_cop(saldo_fondo(estado, 'gastos_personales'))}*")
    lineas += ["", construir_bolsillos(estado)]
    return "\n".join(lineas)

async def deshacer_ultimo_gasto(estado: dict) -> Optional[str]:
    """Borra la última fila de Sheets y revierte su impacto donde se aplicó
    (bolsillo, o fondo + semana donde se registró).
    Devuelve el mensaje de confirmación, o None si no había ningún gasto que borrar."""
    fila = borrar_ultimo_gasto()
    if fila is None:
        return None

    monto = parsear_monto(str(fila.get("monto", "")))
    categoria = str(fila.get("categoria", "")).strip().lower()
    medio = str(fila.get("medio", "")).strip().lower()
    nota = str(fila.get("nota", "")).strip()
    fecha = str(fila.get("fecha", ""))

    if monto:
        destino = destino_gasto(estado, categoria, medio)
        mover_gasto(estado, monto, destino, fecha, signo=-1)
        if destino.startswith("bolsillo:"):
            clave = destino.split(":", 1)[1]
            registrar_movimiento(str(hoy_colombia()), clave, estado["bolsillos"][clave]["nombre"], "ingreso", monto, "Gasto borrado")

    mensaje = f"🗑️ Borrado: `{formato_cop(monto or 0)}` · {categoria} · {medio or 'sin medio'}"
    if nota:
        mensaje += f" · _{nota}_"
    mensaje += "\n\nEscribe el gasto correcto cuando quieras."
    return mensaje

async def crear_bolsillo(estado: dict, texto: str) -> str:
    """'nuevo bolsillo Viajes 500000' (el saldo inicial es opcional)."""
    resto = re.sub(r"^\W*nuevo bolsillo", "", texto, flags=re.IGNORECASE)
    m = re.search(r"\$?\s*(\d[\d.,]*)\s*$", resto)
    saldo = parsear_monto(m.group(1)) if m else 0
    nombre = (resto[:m.start()] if m else resto).strip(" :=-\n")
    if not nombre:
        return "¿Cómo se llama el bolsillo? 😅\n_Ej: nuevo bolsillo Viajes 500000_"
    nombre = nombre.title()
    clave = slug(nombre)
    if clave in estado.setdefault("bolsillos", {}):
        return f"Ya tienes un bolsillo *{estado['bolsillos'][clave]['nombre']}* 👛"
    estado["bolsillos"][clave] = {"nombre": nombre, "saldo": saldo}
    if saldo:
        registrar_movimiento(str(hoy_colombia()), clave, nombre, "ingreso", saldo, "Saldo inicial")
    return f"✅ Bolsillo creado: *{nombre}* · `{formato_cop(saldo)}`"

async def mover_bolsillo(estado: dict, texto: str) -> str:
    """Ingreso o retiro manual: 'ingreso a mercado 50000', 'retiro de mascota 20.000'."""
    tipo = "ingreso" if re.search(r"^\W*ingres", texto, re.IGNORECASE) else "retiro"
    m = re.search(r"\$?\s*(\d[\d.,]*)", texto)
    monto = parsear_monto(m.group(1)) if m else None
    clave = buscar_bolsillo(estado, texto[:m.start()] if m else texto)
    if monto is None or clave is None:
        disponibles = ", ".join(b["nombre"] for b in estado.get("bolsillos", {}).values()) or "ninguno todavía"
        return (
            "No entendí 😅 Escribe el bolsillo y el monto.\n"
            f"_Ej: ingreso a mercado 50000 · retiro de mascota 20000_\nBolsillos: {disponibles}"
        )
    bolsillo = estado["bolsillos"][clave]
    bolsillo["saldo"] += monto if tipo == "ingreso" else -monto
    registrar_movimiento(str(hoy_colombia()), clave, bolsillo["nombre"], tipo, monto, "Manual")
    verbo = "Ingreso a" if tipo == "ingreso" else "Retiro de"
    return f"✅ {verbo} *{bolsillo['nombre']}*: `{formato_cop(monto)}`\n👛 Saldo: *{formato_cop(bolsillo['saldo'])}*"


# ── Preguntas pendientes ──────────────────────────────────────────────────────
# Cuando el bot necesita una respuesta (remanente, buffer, día de nómina) envía
# la pregunta, guarda estado["pendiente"] = {"tipo": ..., ...} y termina. La
# respuesta llega después como un mensaje normal y procesar_mensaje la enruta
# aquí; no hay ventana de espera, y mientras tanto el bot sigue registrando
# gastos (el estado sigue en "listo"). Los comandos y gastos tienen prioridad
# sobre la respuesta pendiente.

NEGACIONES = re.compile(r"^\W*(no|nada|ninguno|ninguna|0|cero)\W*$", re.IGNORECASE)

async def pedir_buffer(estado: dict, semana: dict) -> None:
    idx = next(i for i, s in enumerate(estado["semanas"]) if s is semana)
    estado["pendiente"] = {"tipo": "buffer", "semana_idx": idx}
    guardar_estado(estado)
    await enviar_mensaje(
        "⚠️ Te pasaste del presupuesto esta semana.\n"
        "¿Cuánto buffer tomamos de la semana siguiente?\n"
        "_50000 / 100000 / 150000_ (o *no* para dejarlo así)"
    )

async def responder_buffer(estado: dict, pendiente: dict, texto: str) -> None:
    if NEGACIONES.match(texto):
        estado.pop("pendiente", None)
        await enviar_mensaje("👌 Sin buffer. Cuida lo que queda de la semana.")
        return

    buffer = parsear_monto(texto)
    if buffer not in (50000, 100000, 150000):
        await enviar_mensaje("Monto no válido. Elige 50000, 100000 o 150000 (o *no*).")
        return  # sigue pendiente

    semanas = estado["semanas"]
    idx = pendiente["semana_idx"]
    estado.pop("pendiente", None)
    if idx + 1 >= len(semanas):
        await enviar_mensaje("No hay semana siguiente para tomar buffer 😔")
        return

    semana, siguiente = semanas[idx], semanas[idx + 1]
    semana["saldo"] += buffer
    siguiente["presupuesto"] -= buffer
    siguiente["saldo"] -= buffer
    await enviar_mensaje(
        f"✅ Buffer aplicado: *+{formato_cop(buffer)}*\n"
        f"Saldo semana actual: *{formato_cop(semana['saldo'])}*\n"
        f"Presupuesto semana siguiente: *{formato_cop(siguiente['presupuesto'])}*"
    )

async def responder_remanente(estado: dict, pendiente: dict, texto: str) -> None:
    if NEGACIONES.match(texto):
        remanente = 0
    elif re.search(r"\d", texto):
        remanente = parsear_monto(texto) or 0
    else:
        await enviar_mensaje("Escribe el monto del remanente, o *no* si no quedó nada.")
        return  # sigue pendiente

    estado.pop("pendiente", None)
    if not remanente:
        await enviar_mensaje("👌 Listo, seguimos con el presupuesto normal de la semana.")
        return
    semana = estado["semanas"][pendiente["semana_idx"]]
    semana["presupuesto"] += remanente
    semana["saldo"] += remanente
    await enviar_mensaje(
        f"✅ Remanente de *{formato_cop(remanente)}* sumado.\n"
        f"💰 Presupuesto de esta semana: *{formato_cop(semana['presupuesto'])}*"
    )

async def responder_dia_nomina(estado: dict, texto: str) -> None:
    dia = parsear_monto(texto)
    if dia is None or not (1 <= dia <= 31):
        await enviar_mensaje("No entendí 😅 Escribe solo el número del día, ej: *17*")
        return  # sigue pendiente
    estado.pop("pendiente", None)
    estado["dia_nomina_config"] = dia
    await enviar_mensaje(f"✅ Día de nómina actualizado a *{dia}* de cada mes.")

# ── Semana en curso ───────────────────────────────────────────────────────────
def semana_en_curso(estado:dict):
    hoy = str(hoy_colombia())
    for s in estado.get("semanas",[]):
        if s["fecha_inicio"] <= hoy <= s["fecha_fin"]:
            return s

# ── Funcion de parseo ───────────────────────────────────────────────────────────
# La categoría ya no se infiere aquí por palabras clave: la decide el
# clasificador con IA (clasificador.clasificar_gasto), y solo si esa llamada
# falla se usa este parseo como respaldo para monto/medio/nota (ver flujo_escucha).

def parsear_gasto_texto(texto: str) -> tuple:
    # Nota libre tras un guion (" - "): se separa antes de parsear monto/medio
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

    return (monto, medio, nota)
 

# ── Registro de un gasto ──────────────────────────────────────────────────────

def interpretar_gasto(estado: dict, texto: str) -> tuple:
    """(monto, categoria, medio, nota) de un mensaje de gasto. Claude interpreta
    el texto; si no responde o falla, se usa el parseo determinista. Nunca
    pregunta nada: si no hay categoría, es 'otros', y si no se dijo el medio
    se queda en None (el gasto saldrá de Gastos personales)."""
    nombres = [b["nombre"] for b in estado.get("bolsillos", {}).values()]
    clasificado = clasificar_gasto(texto, nombres) or {}
    monto = clasificado.get("monto")
    categoria = clasificado.get("categoria")
    medio = clasificado.get("medio")
    nota = clasificado.get("nota")

    if monto is None or medio is None:
        monto_r, medio_r, nota_r = parsear_gasto_texto(texto)
        monto = monto if monto is not None else monto_r
        medio = medio if medio is not None else medio_r
        nota = nota if nota is not None else nota_r

    if not categoria:
        clave = buscar_bolsillo(estado, re.split(r"\s+-\s+", texto, maxsplit=1)[0])
        categoria = estado["bolsillos"][clave]["nombre"] if clave else "otros"
    return monto, categoria.strip().lower(), medio, nota

def construir_confirmacion_gasto(estado: dict, monto: int, categoria: str, destino: str, nota: Optional[str]) -> str:
    lineas = [
        "✅ *Gasto registrado*",
        f"`{formato_cop(monto)}` · {categoria.capitalize()} · {etiqueta_destino(destino)}",
    ]
    if nota:
        lineas.append(f"_{nota}_")

    if destino.startswith("bolsillo:"):
        bolsillo = estado["bolsillos"][destino.split(":", 1)[1]]
        lineas.append(f"👛 Saldo {bolsillo['nombre']}: *{formato_cop(bolsillo['saldo'])}*")
        return "\n".join(lineas)

    if "fondos" in estado:
        lineas.append(f"💳 Saldo Rappicard: *{formato_cop(saldo_fondo(estado, 'rappicard'))}*")
    semana = semana_en_curso(estado)
    if semana:
        lineas.append(f"📅 Saldo de la semana: *{formato_cop(semana['saldo'])}*")
    if "fondos" in estado:
        lineas.append(f"🎉 Saldo gastos personales: *{formato_cop(saldo_fondo(estado, 'gastos_personales'))}*")
    return "\n".join(lineas)

async def procesar_gasto(estado: dict, texto: str) -> None:
    monto, categoria, medio, nota = interpretar_gasto(estado, texto)

    if not monto:
        await enviar_mensaje(
            "No entendí ese gasto 😅\n"
            "Formato: _hoy gasté 25000 en domicilios con rappicard_\n"
            "Con nota (opcional): _hoy gasté 25000 en salidas con rappicard - cumpleaños de Ana_"
        )
        return

    destino = destino_gasto(estado, categoria, medio)
    hoy = str(hoy_colombia())
    registrar_gasto(hoy, monto, categoria, medio or "", nota or "")
    mover_gasto(estado, monto, destino, hoy)
    if destino.startswith("bolsillo:"):
        clave = destino.split(":", 1)[1]
        registrar_movimiento(hoy, clave, estado["bolsillos"][clave]["nombre"], "retiro", monto, nota or categoria)
    guardar_estado(estado)

    await enviar_mensaje(construir_confirmacion_gasto(estado, monto, categoria, destino, nota))

    # Las alertas de presupuesto semanal solo aplican a Rappicard + gastos personales
    semana = semana_en_curso(estado)
    if destino.startswith("bolsillo:") or not semana:
        return

    if semana["saldo"] < 0:
        await pedir_buffer(estado, semana)
        return

    # Alerta proactiva: una sola vez por semana al cruzar el 70% del presupuesto
    if not semana.get("alerta_70") and semana["presupuesto"] > 0 and semana["gastado"] >= semana["presupuesto"] * 0.7:
        semana["alerta_70"] = True
        guardar_estado(estado)
        pct = round(semana["gastado"] / semana["presupuesto"] * 100)
        await enviar_mensaje(
            f"🚨 *Ojo:* ya llevas el *{pct}%* del presupuesto de la {semana['label']}.\n"
            f"Te quedan *{formato_cop(semana['saldo'])}* hasta el {_fmt(date.fromisoformat(semana['fecha_fin']))}. "
            "Modo cabeza fría 🧊"
        )
        print(f"🚨 Alerta 70% enviada ({pct}%)")

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
    # sin esto, una segunda ejecución vuelve a anunciar la semana y a preguntar el remanente.
    if semana.get("anunciada"):
        print(f"⏭️  {semana['label']} ya fue anunciada hoy. Sin acción.")
        return
    semana["anunciada"] = True
    guardar_estado(estado)

    fecha_pago = date.fromisoformat(estado["fecha_nomina"])
    total = sum(s["presupuesto"] for s in semanas)

    # Ajuste de mitad de ciclo: en Semana 3, en vez de la pregunta genérica de
    # remanente, se pide la plata REAL en Rappicard y gastos personales y se rehacen
    # Semana 3, Semana 4 y el Cierre a partir de eso (ver procesar_ajuste_medios).
    if idx == 2:
        fin_ciclo = date.fromisoformat(semanas[-1]["fecha_fin"])
        await enviar_mensaje(
            "🔄 *¡Arrancamos la Semana 3!* Antes del presupuesto, hagamos un ajuste con plata real.\n\n"
            "¿Cuánto tienes disponible *ahora mismo* en Rappicard y en gastos personales?\n"
            "Escribe los dos montos separados por espacio: primero Rappicard, luego gastos personales.\n"
            "_Ej: 400000 120000_"
        )
        estado["estado"] = "esperando_ajuste_medios"
        estado["ajuste_fecha_inicio"] = semana["fecha_inicio"]
        estado["ajuste_fin_ciclo"] = str(fin_ciclo)
        estado["ajuste_idx_inicio"] = idx
        estado["ajuste_n_semanas"] = len(semanas) - 1 - idx
        guardar_estado(estado)
        print("⏳ Esperando ajuste de medios (Rappicard/gastos personales) para Semana 3.")
        return

    anterior = semanas[idx - 1]
    dia_nomina_config = estado.get("dia_nomina_config", DIA_NOMINA_DEFAULT)
    await enviar_mensaje(construir_mensaje_semana(semana, total, fecha_pago, dia_nomina_config))

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

    estado["pendiente"] = {"tipo": "remanente", "semana_idx": idx}
    guardar_estado(estado)
    print("⏳ Pregunta de remanente enviada.")

# ── Ajuste manual (bajo demanda) ───────────────────────────────────────────────
# A diferencia del checkpoint automático de la Semana 3 (ver flujo_semanal),
# esto se dispara a mano (workflow_dispatch modo=ajuste) cualquier día del
# ciclo. Recalcula desde la semana en curso (si hay una activa hoy) o, si no,
# desde la próxima semana que aún no ha empezado, más el Cierre, con la plata
# real que el usuario reporte en ese momento.

async def flujo_ajuste_manual() -> None:
    estado = leer_estado()
    if estado.get("estado") != "listo" or not estado.get("semanas"):
        print("⏭️  No hay ciclo configurado. Sin acción.")
        return

    hoy = str(hoy_colombia())
    semanas = estado["semanas"]

    idx = None
    for i, s in enumerate(semanas):
        if s["label"] != "Cierre del ciclo" and s["fecha_fin"] >= hoy:
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
        "¿Cuánto tienes disponible *ahora mismo* en Rappicard y en gastos personales?\n"
        "Escribe los dos montos separados por espacio: primero Rappicard, luego gastos personales.\n"
        "_Ej: 400000 120000_"
    )
    estado["estado"] = "esperando_ajuste_medios"
    estado["ajuste_fecha_inicio"] = str(fecha_inicio)
    estado["ajuste_fin_ciclo"] = str(fin_ciclo)
    estado["ajuste_idx_inicio"] = idx
    estado["ajuste_n_semanas"] = n_semanas
    guardar_estado(estado)
    print(f"⏳ Pregunta de ajuste enviada (desde {semanas[idx]['label']}).")

# ── Dispatcher de mensajes ────────────────────────────────────────────────────
# Único punto de entrada para CUALQUIER mensaje del usuario, venga del webhook
# de Cloud Run o del polling manual (flujo_escucha). Muta `estado`; quien lo
# llama es responsable de guardarlo.

async def procesar_mensaje(estado: dict, texto: str) -> bool:
    """Devuelve True si el mensaje se entendió y se procesó, False si se ignoró."""
    dia_nomina_config = estado.get("dia_nomina_config", DIA_NOMINA_DEFAULT)
    modo = estado.get("estado")

    # Estados que esperan una respuesta concreta y no son "listo": el mensaje
    # es la respuesta (si no se entiende, el handler vuelve a preguntar).
    if modo in ("esperando_presupuestos", "esperando_presupuestos_categoria"):
        await procesar_presupuestos(estado, texto, date.fromisoformat(estado["fecha_nomina"]), dia_nomina_config)
        return True
    if modo == "esperando_ajuste_medios":
        await procesar_ajuste_medios(
            estado, texto,
            date.fromisoformat(estado["ajuste_fecha_inicio"]),
            date.fromisoformat(estado["ajuste_fin_ciclo"]),
            estado.get("ajuste_idx_inicio", 2),
            estado.get("ajuste_n_semanas", 2),
        )
        return True
    if modo != "listo":
        return False

    # Comandos y gastos: tienen prioridad sobre una pregunta pendiente
    if es_ajuste_dia_nomina(texto):
        estado["pendiente"] = {"tipo": "dia_nomina"}
        await enviar_mensaje("¿Cuál es tu día de nómina? (número del 1 al 31)")
    elif es_borrar_gasto(texto):
        await enviar_mensaje(await deshacer_ultimo_gasto(estado) or "No hay gastos para borrar 🤷")
    elif es_nuevo_bolsillo(texto):
        await enviar_mensaje(await crear_bolsillo(estado, texto))
    elif es_movimiento_bolsillo(texto):
        await enviar_mensaje(await mover_bolsillo(estado, texto))
    elif es_consulta_saldos(texto):
        await enviar_mensaje(construir_saldos(estado))
    elif es_reporte_gasto(texto):
        await procesar_gasto(estado, texto)
    elif estado.get("pendiente"):
        pendiente = estado["pendiente"]
        if pendiente["tipo"] == "buffer":
            await responder_buffer(estado, pendiente, texto)
        elif pendiente["tipo"] == "remanente":
            await responder_remanente(estado, pendiente, texto)
        elif pendiente["tipo"] == "dia_nomina":
            await responder_dia_nomina(estado, texto)
    else:
        return False
    return True

# ── Escucha por polling (respaldo manual) ─────────────────────────────────────
# Con el webhook de Cloud Run activo esto NO se puede usar: Telegram solo
# permite webhook o getUpdates, nunca ambos (getUpdates responde 409). Sirve
# antes de la migración y como respaldo tras un deleteWebhook.

async def flujo_escucha() -> None:
    estado = leer_estado()
    offset = estado.get("ultimo_update_id", 0) + 1
    procesados = 0

    async with httpx.AsyncClient() as client:
        updates = await obtener_updates(client, offset)

    for update in updates:
        estado["ultimo_update_id"] = update["update_id"]

        msg = update.get("message", {})
        if str(msg.get("chat", {}).get("id")) != str(CHAT_ID):
            continue

        if await procesar_mensaje(estado, msg.get("text", "")):
            procesados += 1
        # Guardar tras cada mensaje: así un fallo en el siguiente no pierde este
        guardar_estado(estado)

    guardar_estado(estado)  # al menos persiste ultimo_update_id
    print(f"✅ {procesados} mensaje(s) procesado(s)" if procesados else "📭 Sin mensajes pendientes.")

# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    hoy = hoy_colombia()

    if "--nomina" in sys.argv:
        estado = leer_estado()
        dia_nomina_config = estado.get("dia_nomina_config", DIA_NOMINA_DEFAULT)
        nomina = dia_nomina(hoy.month, hoy.year, dia_nomina_config)
        if hoy != nomina and "--forzar" not in sys.argv:
            print(f"⏭️  Hoy ({hoy}) no es día de nómina ({nomina}). Sin acción.")
            return
        await flujo_nomina(hoy, forzar="--forzar" in sys.argv)

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
        estado = leer_estado()
        dia_nomina_config = estado.get("dia_nomina_config", DIA_NOMINA_DEFAULT)
        mensaje = construir_insights(leer_gastos(), dia_nomina_config)
        if mensaje:
            await enviar_mensaje(mensaje)
            print("✅ Insights enviados")
        else:
            print("⏭️  Aún no hay 2 ciclos completos de datos. Sin acción.")

    elif "--ajuste" in sys.argv:
        await flujo_ajuste_manual()

    else:
        await flujo_semanal()

if __name__ == "__main__":
    asyncio.run(main())
