"""Configuración sin credenciales incrustadas; los valores vienen del entorno del runner."""
import os

TELEGRAM_BOT_TOKEN = os.environ.get("PUBLISHER_TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("PUBLISHER_TELEGRAM_CHAT_ID", "")
ID_ENTRADA = os.environ.get("DRIVE_FOLDER_INPUT", "")
ID_PROCESADAS = os.environ.get("DRIVE_FOLDER_PROCESSED", "")
ID_PUBLICAR = os.environ.get("DRIVE_FOLDER_OUTPUT", "")
GANANCIA_PORCENTAJE = float(os.environ.get("GANANCIA_PORCENTAJE", "20"))
ESCANEO_DRIVE_SEGUNDOS = int(os.environ.get("ESCANEO_DRIVE_SEGUNDOS", "15"))
