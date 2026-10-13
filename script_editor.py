# -*- coding: utf-8 -*-
"""Análisis visual de folletos para Revendedor WA.

Usa Gemini API si GEMINI_API_KEY está configurada y, como respaldo, OpenRouter
si OPENROUTER_API_KEY está configurada. Nunca contiene claves incrustadas.
La edición gráfica final se realiza localmente con Pillow en procesar_imagenes.py.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import time
from typing import Any

import requests
from PIL import Image, ImageOps

log = logging.getLogger("revendedor.ia")

PROMPT = """Analizá esta imagen para preparar una publicación comercial en Argentina.
También recibís texto OCR, que puede contener errores. La imagen es la fuente principal.
Respondé SOLO JSON válido, sin Markdown, con estas claves:
{
 "nombre_producto": "nombre/modelo visible o vacío",
 "precio_contado": número entero o null,
 "moneda": "ARS" o "USD",
 "modalidad_precio": "efectivo", "contado", "transferencia", "financiado", "cuotas" o "desconocido",
 "confianza": número de 0 a 1,
 "detalle_producto": "características que realmente se lean o se vean, sin inventar",
 "texto_publicitario": "descripción breve, atractiva y honesta en español rioplatense"
}
Reglas estrictas:
- Nunca confundas cuotas, precio financiado, tarjeta, precio de lista ni total de cuotas con el precio de contado.
- Si hay un importe claramente rotulado EFECTIVO o CONTADO, ese es el precio contado.
- Si no se distingue de forma fiable el precio de contado, usa null; no inventes ni adivines.
- No confundas códigos de modelo, cantidades, medidas o números de cuotas con precios.
- Si la imagen es solo una foto limpia del producto sin precio, precio_contado debe ser null.
- No inventes marca, potencia, capacidad, garantía, stock ni prestaciones.
- El nombre del producto puede inferirse visualmente solo si es inequívoco.
- texto_publicitario no debe mencionar el precio si no estás seguro del importe.
Texto OCR auxiliar:
"""

def _imagen_para_api(buffer_imagen) -> tuple[str, str]:
    buffer_imagen.seek(0)
    img = ImageOps.exif_transpose(Image.open(buffer_imagen)).convert("RGB")
    img.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=88, optimize=True)
    buffer_imagen.seek(0)
    return base64.b64encode(out.getvalue()).decode("ascii"), "image/jpeg"

def _parsear_json(texto: str) -> dict[str, Any] | None:
    texto = (texto or "").strip()
    inicio, fin = texto.find("{"), texto.rfind("}")
    if inicio < 0 or fin <= inicio:
        return None
    try:
        dato = json.loads(texto[inicio:fin + 1])
        if not isinstance(dato, dict):
            return None
        precio = dato.get("precio_contado")
        if isinstance(precio, str):
            digitos = re.sub(r"\D", "", precio)
            precio = int(digitos) if digitos else None
        elif isinstance(precio, (int, float)) and not isinstance(precio, bool):
            precio = int(precio)
        else:
            precio = None
        if precio is not None and not 100 <= precio <= 100_000_000_000:
            precio = None
        try:
            confianza = max(0.0, min(1.0, float(dato.get("confianza", 0))))
        except (TypeError, ValueError):
            confianza = 0.0
        modalidad = str(dato.get("modalidad_precio", "desconocido")).strip().lower()
        if modalidad not in {"efectivo", "contado", "transferencia", "financiado", "cuotas", "desconocido"}:
            modalidad = "desconocido"
        if modalidad in {"financiado", "cuotas"}:
            precio = None
        return {
            "nombre_producto": str(dato.get("nombre_producto", "")).strip()[:100],
            "precio_contado": precio,
            "moneda": "USD" if str(dato.get("moneda", "ARS")).upper() == "USD" else "ARS",
            "modalidad_precio": modalidad,
            "confianza": confianza,
            "detalle_producto": str(dato.get("detalle_producto", "")).strip()[:500],
            "texto_publicitario": str(dato.get("texto_publicitario", "")).strip()[:900],
        }
    except (ValueError, TypeError):
        return None

def analizar_flyer_con_ia(buffer_imagen, texto_ocr: str = "") -> dict[str, Any] | None:
    """Analiza visualmente una imagen; devuelve None si no hay API disponible."""
    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
    openrouter_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not gemini_key and not openrouter_key:
        log.warning("No hay GEMINI_API_KEY ni OPENROUTER_API_KEY; se usará OCR como respaldo.")
        return None
    try:
        image_b64, mime = _imagen_para_api(buffer_imagen)
    except Exception:
        log.exception("No pude preparar la imagen para el análisis visual de IA.")
        return None
    prompt = PROMPT + (texto_ocr or "(sin texto OCR)")[:9000]
    if gemini_key:
        try:
            # Gemini 3.5 Flash-Lite no admite el parámetro de muestreo temperature.
            # Enviarlo puede rechazar la petición y dejar al sistema usando solo OCR.
            url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent"
            response = None
            for intento in range(3):
                response = requests.post(
                    url,
                    params={"key": gemini_key},
                    json={"contents": [{"parts": [
                        {"text": prompt},
                        {"inline_data": {"mime_type": mime, "data": image_b64}},
                    ]}], "generationConfig": {"responseMimeType": "application/json"}},
                    timeout=45,
                )
                if response.status_code not in (408, 429, 500, 502, 503, 504) or intento == 2:
                    break
                espera = min(20, 2 ** (intento + 1))
                log.warning(
                    "Gemini respondió HTTP %s al analizar la imagen; reintento %s/3 en %ss.",
                    response.status_code, intento + 2, espera
                )
                time.sleep(espera)
            response.raise_for_status()
            partes = response.json().get("candidates", [{}])[0].get("content", {}).get("parts", [])
            texto = "\n".join(str(p.get("text", "")) for p in partes)
            dato = _parsear_json(texto)
            if dato:
                dato["proveedor_ia"] = "Gemini"
                log.info("Análisis visual completado con Gemini.")
                return dato
            log.warning("Gemini respondió sin un JSON interpretable; se probará el respaldo si está configurado.")
        except Exception as exc:
            log.warning("Falló Gemini; se probará OpenRouter si hay clave: %s", exc)
    if openrouter_key:
        # Solo modelo marcado como gratuito: nunca intentar un modelo de pago como respaldo.
        modelos = ["qwen/qwen-2.5-vl-72b-instruct:free"]
        for modelo in modelos:
            try:
                response = requests.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {openrouter_key}",
                        "Content-Type": "application/json",
                        "HTTP-Referer": "https://github.com/cholovic1-arch/revendedor-wa-cloud",
                        "X-Title": "Revendedor WA",
                    },
                    json={"model": modelo, "temperature": 0.1, "max_tokens": 700, "messages": [{
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{image_b64}"}},
                        ],
                    }]},
                    timeout=45,
                )
                if response.status_code in (401, 402, 404, 429, 500, 502, 503):
                    log.warning("OpenRouter modelo %s devolvió HTTP %s.", modelo, response.status_code)
                    continue
                response.raise_for_status()
                texto = response.json()["choices"][0]["message"]["content"]
                if isinstance(texto, list):
                    texto = "\n".join(str(p.get("text", "")) for p in texto if isinstance(p, dict))
                dato = _parsear_json(str(texto))
                if dato:
                    dato["proveedor_ia"] = f"OpenRouter/{modelo}"
                    log.info("Análisis visual completado con %s.", modelo)
                    return dato
            except Exception as exc:
                log.warning("Falló el análisis visual con OpenRouter/%s: %s", modelo, exc)
    log.error("No se pudo completar el análisis visual con los proveedores de IA configurados.")
    return None

def generar_texto_publicitario(nombre_producto: str, precios: list[dict], detalle: str = "") -> str:
    """Descripción comercial segura; no agrega prestaciones no confirmadas."""
    nombre = (nombre_producto or "Producto").strip()
    principal = next((p for p in (precios or []) if str(p.get("etiqueta", "")).lower() in ("contado", "efectivo", "cash")), None)
    if principal is None and precios:
        principal = precios[0]
    valor = int((principal or {}).get("venta", 0) or 0)
    precio = "$" + f"{valor:,}".replace(",", ".") if valor else "consultar precio"
    lineas = [f"¡Conocé {nombre}!", f"Precio de venta: {precio}."]
    if detalle:
        lineas.append(detalle.strip())
    lineas.append("Consultanos por disponibilidad y envío. Escribinos para más información.")
    return "\n".join(lineas)[:1000]
