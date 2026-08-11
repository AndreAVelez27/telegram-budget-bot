import gspread
from google.oauth2.service_account import Credentials
import json
import os
import base64
from dotenv import load_dotenv

load_dotenv()

def get_sheet(): 
    raw = os.getenv("GOOGLE_CREDENTIALS_B64")
    sheet_id = os.getenv("GOOGLE_SHEETS_ID")

    decoded_credentials = base64.b64decode(raw).decode('utf-8')
    credentials_dict = json.loads(decoded_credentials)
    creds = Credentials.from_service_account_info(
        credentials_dict,
        scopes=["https://www.googleapis.com/auth/spreadsheets"])
    client = gspread.authorize(creds) 
    sheet = client.open_by_key(sheet_id)
    return sheet.worksheet("gastos")

def registrar_gasto(fecha, monto, categoria, medio, nota=""):
    sheet = get_sheet()
    sheet.append_row([fecha, monto, categoria, medio, nota])

def leer_gastos():
    """Devuelve todos los gastos como lista de dicts (una fila = un dict)."""
    sheet = get_sheet()
    return sheet.get_all_records()





