# Telegram Budget Bot 💚

Bot personal que envía recordatorios de presupuesto semanal por Telegram, automatizado con GitHub Actions.

## Qué hace

- Cada **sábado a las 8am** envía el presupuesto de la semana en curso.
- Cada **día 27 del mes** envía el resumen completo del ciclo de pago.
- Se puede lanzar manualmente desde GitHub Actions en cualquier momento.

## Setup

### 1. Clonar e instalar dependencias

```bash
git clone https://github.com/tu-usuario/telegram-budget-bot.git
cd telegram-budget-bot
pip install -r requirements.txt
```

### 2. Configurar variables de entorno

Copia `.env.example` a `.env` y rellena tus valores:

```bash
cp .env.example .env
```

```env
TELEGRAM_TOKEN=tu_token_de_botfather
TELEGRAM_CHAT_ID=tu_chat_id
```

### 3. Probar localmente

```bash
# Presupuesto semanal
python3 budget_bot.py

# Resumen del ciclo completo
python3 budget_bot.py --resumen
```

### 4. Automatizar con GitHub Actions

En tu repositorio ve a **Settings → Secrets and variables → Actions** y agrega:

| Secret | Valor |
|---|---|
| `TELEGRAM_TOKEN` | El token que te dio BotFather |
| `TELEGRAM_CHAT_ID` | Tu chat ID de Telegram |

El workflow en `.github/workflows/budget.yml` se encarga del resto.

## Obtener tu Chat ID

Habla con [@userinfobot](https://t.me/userinfobot) en Telegram — te responde con tu ID.
