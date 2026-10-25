import os
from typing import Optional

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()

MODELO = "claude-haiku-4-5-20251001"

_cliente: Optional[Anthropic] = None


def _obtener_cliente() -> Optional[Anthropic]:
    global _cliente
    if _cliente is None:
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            return None
        _cliente = Anthropic(api_key=api_key)
    return _cliente


HERRAMIENTA = {
    "name": "registrar_gasto",
    "description": "Extrae los datos de un gasto a partir de un mensaje libre en español.",
    "input_schema": {
        "type": "object",
        "properties": {
            "monto": {"type": "integer", "description": "Monto del gasto en pesos colombianos, sin puntos ni símbolos."},
            "categoria": {"type": "string", "description": "Si el gasto corresponde a uno de los bolsillos dados, el nombre EXACTO de ese bolsillo. Si no, una etiqueta corta en minúsculas que describa el gasto (ej. domicilios, salidas, ropa)."},
            "medio": {"type": "string", "enum": ["rappicard", "efectivo", "otro"], "description": "Medio de pago SOLO si el mensaje lo menciona. Omítelo si no se dice."},
            "nota": {"type": "string", "description": "Nota libre adicional (ej. para quién o por qué), si la hay."},
        },
        "required": ["monto", "categoria"],
    },
}


def clasificar_gasto(texto: str, bolsillos: list) -> Optional[dict]:
    """Usa Claude para extraer monto/categoría/medio/nota de un mensaje de
    gasto en texto libre (ej. 'Hoy gasté 10.000 en domicilios con rappicard').
    `bolsillos` son los nombres de los bolsillos del usuario: si el gasto
    corresponde a uno, `categoria` devuelve ese nombre exacto; si no, una
    etiqueta libre (la categoría ya no es una lista cerrada, solo sirve para
    reportes). Devuelve None si no hay API key, la llamada falla o no hay
    monto — el llamador hace fallback a parseo determinista."""
    cliente = _obtener_cliente()
    if cliente is None:
        return None

    lista = ", ".join(bolsillos) if bolsillos else "(ninguno)"
    try:
        respuesta = cliente.messages.create(
            model=MODELO,
            max_tokens=256,
            tools=[HERRAMIENTA],
            tool_choice={"type": "tool", "name": "registrar_gasto"},
            messages=[{
                "role": "user",
                "content": (
                    f"Bolsillos del usuario: {lista}.\n"
                    f"Mensaje del usuario: {texto!r}\n"
                    "Extrae el gasto con la herramienta registrar_gasto. "
                    "No inventes el medio de pago: si el mensaje no lo menciona, omítelo."
                ),
            }],
        )
    except Exception as e:
        print(f"⚠️ Clasificador: error llamando a Claude: {e}")
        return None

    for bloque in respuesta.content:
        if bloque.type == "tool_use" and bloque.name == "registrar_gasto":
            datos = dict(bloque.input)
            if not datos.get("monto"):
                return None
            return datos

    return None
