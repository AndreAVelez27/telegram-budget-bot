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
            "categoria": {"type": "string", "description": "Una de las categorías dadas, la que mejor describa el gasto."},
            "medio": {"type": "string", "enum": ["rappicard", "efectivo"], "description": "Medio de pago, si se menciona o se puede inferir."},
            "nota": {"type": "string", "description": "Nota libre adicional (ej. para quién o por qué), si la hay."},
        },
        "required": ["monto", "categoria"],
    },
}


def clasificar_gasto(texto: str, categorias: list) -> Optional[dict]:
    """Usa Claude para extraer monto/categoría/medio/nota de un mensaje de
    gasto en texto libre (ej. 'Hoy gasté 10.000 carro - cumple juanita').
    `categorias` es la lista de claves válidas (CATEGORIAS_LABELS.keys()).
    Devuelve None si no hay API key configurada, la llamada falla, o la
    categoría devuelta no está en `categorias` — el llamador hace fallback."""
    cliente = _obtener_cliente()
    if cliente is None:
        return None

    try:
        respuesta = cliente.messages.create(
            model=MODELO,
            max_tokens=256,
            tools=[HERRAMIENTA],
            tool_choice={"type": "tool", "name": "registrar_gasto"},
            messages=[{
                "role": "user",
                "content": (
                    f"Categorías válidas: {', '.join(categorias)}.\n"
                    f"Mensaje del usuario: {texto!r}\n"
                    "Extrae el gasto con la herramienta registrar_gasto. "
                    "La categoría debe ser exactamente una de las categorías válidas."
                ),
            }],
        )
    except Exception as e:
        print(f"⚠️ Clasificador: error llamando a Claude: {e}")
        return None

    for bloque in respuesta.content:
        if bloque.type == "tool_use" and bloque.name == "registrar_gasto":
            datos = dict(bloque.input)
            if datos.get("categoria") not in categorias:
                return None
            return datos

    return None
