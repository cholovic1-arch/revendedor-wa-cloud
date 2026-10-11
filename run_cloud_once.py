"""Punto de entrada efímero para GitHub Actions; no mantiene procesos en bucle."""
import os
os.environ["REVENDOR_CLOUD_MODE"] = "1"

from bot_telegram import ejecutar_ciclo_cloud

if __name__ == "__main__":
    ejecutar_ciclo_cloud()
