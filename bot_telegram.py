# -*- coding: utf-8 -*-
"""Monitor de Drive y revisión de carteles en Telegram para Revendedor WA."""
from __future__ import annotations
import io, json, logging, re, time, uuid, threading, os
from pathlib import Path
import requests
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload
import procesar_imagenes as pi
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, ID_ENTRADA, ID_PUBLICAR, GANANCIA_PORCENTAJE, ESCANEO_DRIVE_SEGUNDOS

BASE = Path(__file__).resolve().parent
STATE_FILE = BASE / "estado_revendedor.json"
API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("revendedor")
state = {"last_update_id": 0, "seen_drive_ids": [], "drafts": {}, "waiting_edit": {}, "pending_drive": {}}
lock = threading.RLock()
REMOTE_STATE_NAME = "revendedor_wa_estado_cloud.json"
REMOTE_STATE_ID = None


def _read_state_from_drive():
    """Carga el estado fuera del runner efímero usando la carpeta privada appData de Drive."""
    global REMOTE_STATE_ID
    service = pi.obtener_servicio_drive()
    found = service.files().list(
        spaces="appDataFolder",
        q=f"name = '{REMOTE_STATE_NAME}' and trashed = false",
        pageSize=1,
        fields="files(id,name)",
    ).execute().get("files", [])
    if not found:
        REMOTE_STATE_ID = None
        return None
    REMOTE_STATE_ID = found[0]["id"]
    buff = io.BytesIO()
    req = service.files().get_media(fileId=REMOTE_STATE_ID)
    downloader = MediaIoBaseDownload(buff, req)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return json.loads(buff.getvalue().decode("utf-8"))


def _write_state_to_drive(payload):
    """Guarda el estado en appData; no expone el archivo en la carpeta de productos."""
    global REMOTE_STATE_ID
    service = pi.obtener_servicio_drive()
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    media = MediaIoBaseUpload(io.BytesIO(raw), mimetype="application/json", resumable=False)
    if REMOTE_STATE_ID:
        service.files().update(fileId=REMOTE_STATE_ID, media_body=media, fields="id").execute()
    else:
        created = service.files().create(
            body={"name": REMOTE_STATE_NAME, "parents": ["appDataFolder"]},
            media_body=media,
            fields="id,name",
        ).execute()
        REMOTE_STATE_ID = created["id"]


def load_state():
    global state
    if os.environ.get("REVENDOR_CLOUD_MODE") == "1":
        try:
            remote = _read_state_from_drive()
            if remote is not None:
                state.update(remote)
                log.info("Estado cargado desde el almacenamiento privado de Google Drive.")
                return
            log.info("No existe estado remoto; se inicializará uno nuevo.")
        except Exception:
            log.exception("No pude cargar el estado remoto; no continúo para evitar perder borradores.")
            raise
    if STATE_FILE.exists():
        try: state.update(json.loads(STATE_FILE.read_text(encoding="utf-8")))
        except Exception: log.exception("No pude leer estado_revendedor.json")
    if os.environ.get("REVENDOR_CLOUD_MODE") == "1":
        save_state()


def save_state():
    with lock:
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        if os.environ.get("REVENDOR_CLOUD_MODE") == "1":
            _write_state_to_drive(state)


def api(method, data=None, files=None, timeout=60):
    """Llama a Telegram reintentando límites 429 y fallos temporales 5xx."""
    ultimo_error = None
    for intento in range(5):
        try:
            r = requests.post(f"{API}/{method}", data=data or {}, files=files, timeout=timeout)
            if r.status_code == 429 or r.status_code in (500, 502, 503, 504):
                espera = min(30, 2 ** intento)
                try:
                    detalle = r.json()
                    espera = min(60, max(espera, int(detalle.get("parameters", {}).get("retry_after", 0))))
                except Exception:
                    pass
                ultimo_error = RuntimeError(f"Telegram {method}: HTTP {r.status_code}; reintento en {espera}s")
                log.warning("%s; intento %s/5.", ultimo_error, intento + 1)
                if intento < 4:
                    time.sleep(espera)
                    continue
                r.raise_for_status()
            r.raise_for_status()
            j = r.json()
            if not j.get("ok"):
                if j.get("error_code") == 429 and intento < 4:
                    espera = min(60, int(j.get("parameters", {}).get("retry_after", 2 ** intento)))
                    log.warning("Telegram limitó %s; reintento en %ss.", method, espera)
                    time.sleep(espera)
                    continue
                raise RuntimeError(j.get("description", f"Telegram {method} falló"))
            return j.get("result")
        except (requests.RequestException, ValueError) as exc:
            ultimo_error = exc
            if intento >= 4:
                raise
            espera = min(30, 2 ** intento)
            log.warning("Error temporal en Telegram %s (%s/5): %s. Reintento en %ss.", method, intento + 1, exc, espera)
            time.sleep(espera)
    raise RuntimeError(f"Telegram {method} no respondió tras 5 intentos: {ultimo_error}")


def enviar_texto(texto, reply_markup=None, chat_id=None):
    data = {"chat_id": str(chat_id or TELEGRAM_CHAT_ID), "text": texto}
    if reply_markup: data["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    return api("sendMessage", data=data)


def enviar_foto(buffer, nombre, caption, keyboard=None, chat_id=None):
    buffer.seek(0); data = {"chat_id": str(chat_id or TELEGRAM_CHAT_ID), "caption": caption[:1024]}
    if keyboard: data["reply_markup"] = json.dumps(keyboard, ensure_ascii=False)
    return api("sendPhoto", data=data, files={"photo": (nombre, buffer, "image/jpeg")}, timeout=90)


def pesos(n): return "$" + f"{int(round(float(n))):,}".replace(",", ".")


def formatear_precio(precio, valor):
    moneda = str(precio.get("moneda", "ARS")).upper()
    importe = f"{int(round(float(valor))):,}".replace(",", ".")
    return f"USD {importe}" if moneda == "USD" else "$" + importe


def texto_cartel(nombre, precios, porcentaje):
    """Texto promocional; conserva moneda y productos del listado."""
    filas = "\n".join(
        f"• {p.get('producto') or p['etiqueta']}: {formatear_precio(p, p['venta'])}"
        for p in precios
    )
    return (
        f"🔥 ¡OFERTAS PARA APROVECHAR! 🔥\n\n"
        f"🛍️ {nombre}\n\n"
        f"{filas}\n\n"
        "📦 Consultá stock y precio de envío por mensaje privado.\n"
        "📩 Escribinos para consultar o reservar."
    )

def publicar_en_facebook(rec):
    """Publica el cartel aprobado como foto en la página configurada."""
    page_id = os.environ.get("FACEBOOK_PAGE_ID", "").strip()
    page_token = os.environ.get("FACEBOOK_PAGE_ACCESS_TOKEN", "").strip()
    if not page_id or not page_token:
        raise RuntimeError("Faltan los secretos FACEBOOK_PAGE_ID y FACEBOOK_PAGE_ACCESS_TOKEN en GitHub Actions.")
    if not rec.get("output_id"):
        raise RuntimeError("El borrador no tiene una imagen final guardada en Google Drive.")
    service = pi.obtener_servicio_drive()
    foto = pi.descargar_imagen_a_memoria(service, rec["output_id"])
    foto.seek(0)
    response = requests.post(
        f"https://graph.facebook.com/v26.0/{page_id}/photos",
        data={"caption": rec.get("caption", ""), "access_token": page_token},
        files={"source": (rec.get("output_name") or "cartel.jpg", foto, "image/jpeg")},
        timeout=90,
    )
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if not response.ok or payload.get("error"):
        error = payload.get("error", {})
        detalle = error.get("message") or f"HTTP {response.status_code}"
        raise RuntimeError(f"Facebook rechazó la publicación: {detalle}")
    return payload.get("post_id") or payload.get("id") or ""


def teclado(did):
    return {"inline_keyboard": [[
        {"text": "📣 Publicar en Facebook", "callback_data": f"ok:{did}"},
        {"text": "✏️ Modificar", "callback_data": f"edit:{did}"},
        {"text": "❌ Cancelar", "callback_data": f"cancel:{did}"}
    ]]}


def _precios_detectados(texto):
    """Devuelve hasta tres importes distintos, sin exigir modalidades ni inventar precios."""
    detectados = pi.extraer_precios(texto or "")
    resultado = []
    vistos = set()
    # Respetar el orden en que aparecen los importes en el OCR; la etiqueta solo se usa
    # cuando el extractor encontró una asociación explícita/probable.
    for p in detectados:
        # La etiqueta debe leerse ANTES de filtrar líneas que también mencionan cuotas.
        # Si la línea contiene contado y cuotas, conservar el importe identificado como contado.
        linea = str(p.get("linea", ""))
        etiqueta = str(p.get("etiqueta", "")).strip()
        etiqueta_norm = etiqueta.lower()
        es_contado = etiqueta_norm in ("efectivo", "contado", "cash")
        # Excluir cuotas por etiqueta OCR aunque la palabra haya quedado en otra línea.
        if etiqueta_norm in ("cuotas", "financiado", "financiación", "financiacion"):
            continue
        if re.search(r"financiad|cuotas?|tarjeta de cr[eé]dito|plan de pago|inter[eé]s", linea, re.I) and not es_contado:
            continue
        try:
            valor = int(p.get("costo", 0))
        except (TypeError, ValueError):
            continue
        if valor <= 0 or valor in vistos:
            continue
        if etiqueta.lower() in ("", "sin identificar", "desconocido", "none"):
            etiqueta = f"Precio {len(resultado) + 1}"
        resultado.append({"etiqueta": etiqueta, "costo": valor, "origen": p.get("linea", "")})
        vistos.add(valor)
        if len(resultado) == 3:
            break
    return resultado


def _productos_texto_flexible(texto):
    """Respaldo para mensajes de promociones escritos sin una plantilla fija."""
    encontrados = []
    vistos = set()
    etiquetas = {"producto", "precio", "efectivo", "contado", "transferencia", "lista"}
    patron = re.compile(
        r"(?P<nombre>.+?)\s*[:：–—-]?\s*"
        r"(?P<precio>(?:US\$|U\$S|\$)?\s*(?:\d{1,3}(?:[.,]\d{3}){1,2}|\d{4,}))"
        r"\s*(?P<moneda>USD|ARS)?\s*(?P<modalidad>efectivo|contado|transferencia)?\s*$",
        re.I,
    )
    for linea in re.split(r"\n+|[;|]+", (texto or "").replace("\r", "")):
        limpia = re.sub(r"\s+", " ", linea).strip(" •-*")
        if not limpia or re.search(r"cuotas?|financiad|tarjeta|inter[eé]s|subtotal|total", limpia, re.I):
            continue
        m = patron.match(limpia)
        if not m:
            continue
        nombre = m.group("nombre").strip(" :：–—-.$")
        if nombre.lower() in etiquetas:
            continue
        if len(nombre) < 3 or nombre.lower() in vistos:
            continue
        importe = int(re.sub(r"\D", "", m.group("precio")) or "0")
        if importe < 1000:
            continue
        moneda_txt = (m.group("moneda") or "").upper()
        precio_txt = (m.group("precio") or "").upper()
        moneda = "USD" if moneda_txt == "USD" or precio_txt.startswith(("US$", "U$S")) else "ARS"
        vistos.add(nombre.lower())
        encontrados.append({"etiqueta": (m.group("modalidad") or "Contado").title(), "producto": nombre[:100], "costo": importe, "moneda": moneda})
        if len(encontrados) >= 18:
            break
    return encontrados

def _productos_de_texto_proveedor(texto):
    """Lee listas de proveedor con precios USD/ARS y aclaraciones después del precio."""
    encontrados = []
    vistos = set()
    precio_patron = re.compile(
        r"(?P<signo>US\$|U\$S|\$)?\s*"
        r"(?P<importe>\d{1,3}(?:[.,]\d{3}){1,2}|\d{2,})"
        r"\s*(?P<moneda>USD|US\$|U\$S|DÓLARES?|DOLARES?)?",
        re.I
    )
    lineas = re.split(r"\n+|\s{2,}", (texto or "").replace("\r", ""))
    for linea in lineas:
        limpia = re.sub(r"\s+", " ", linea).strip().strip("•-* ")
        if not limpia or re.search(r"\b(total|subtotal|suma)\b", limpia, re.I):
            continue
        if re.match(r"(?i)^\s*d[oó]lar\b", limpia):
            continue
        candidatos = []
        for m in precio_patron.finditer(limpia):
            valor = int(re.sub(r"\D", "", m.group("importe")) or "0")
            moneda_txt = (m.group("moneda") or "").upper()
            signo = (m.group("signo") or "").upper()
            marcado = bool(signo or moneda_txt)
            if valor > 0 and (marcado or valor >= 1000):
                es_usd = moneda_txt in ("USD", "US$", "U$S", "DÓLAR", "DÓLARES", "DOLAR", "DOLARES") or signo in ("US$", "U$S")
                candidatos.append((m, valor, "USD" if es_usd else "ARS", marcado))
        marcados = [c for c in candidatos if c[3]]
        if marcados:
            elegido = marcados[-1]
        else:
            al_final = [c for c in candidatos if not limpia[c[0].end():].strip(" *).,:;")]
            if not al_final:
                continue
            elegido = al_final[-1]
        m, importe, moneda, _ = elegido
        nombre = limpia[:m.start()].strip(" -:;$*")
        if nombre.rstrip(":：").strip().lower() in {"producto", "precio", "efectivo", "contado", "transferencia", "lista"}:
            continue
        nombre = re.sub(r"[*_]+", "", nombre)
        nombre = re.sub(r"\s+", " ", nombre).strip(" -:;$")
        if len(nombre) < 3 or nombre.lower() in vistos:
            continue
        vistos.add(nombre.lower())
        encontrados.append({"etiqueta": "Contado", "producto": nombre[:100], "costo": importe, "moneda": moneda})
    return encontrados[:18]


def _precios_etiquetados(texto):
    """Compatibilidad: devuelve modalidades explícitas reconocidas por OCR."""
    mapa = {}
    for p in pi.extraer_precios(texto):
        if p["etiqueta"] in ("Lista", "Efectivo", "Transferencia"):
            mapa.setdefault(p["etiqueta"], p["costo"])
    return mapa


def _foto_y_texto(service, lote):
    """Separa la foto limpia del producto de la imagen de proveedor con precios."""
    items = []
    for f in lote:
        buff = pi.descargar_imagen_a_memoria(service, f["id"])
        ocr = pi.leer_texto_ocr(buff)
        low = ocr.lower()
        precios = pi.extraer_precios(ocr)
        señales_precio = sum(1 for k in ("precio de lista", "efectivo", "contado", "transferencia", "precio", "lista") if k in low)
        items.append({"file": f, "ocr": ocr, "precios": precios,
                      "score_precio": señales_precio * 8 + len(precios) * 10 + low.count("$") * 5,
                      "buff": pi.descargar_imagen_a_memoria(service, f["id"])})
    if not items:
        raise ValueError("No hay imágenes para procesar")
    # Editar la foto con menos señales de lista de precios; extraer datos de ambas.
    foto = min(items, key=lambda x: (x["score_precio"], len(x["ocr"])))
    texto = "\n\n".join(x["ocr"] for x in sorted(items, key=lambda x: x["score_precio"], reverse=True))
    nombres = [x["file"].get("name", "imagen") for x in items]
    return foto["buff"], foto["file"]["id"], nombres, texto

def _crear_borrador(service, lote, chat_id):
    foto, foto_id, nombres, texto_ocr = _foto_y_texto(service, lote)
    # Las leyendas escritas junto a las fotos son datos de entrada; no descartarlas.
    captions = list(dict.fromkeys(
        str((f or {}).get("caption", "")).strip()
        for f in lote if str((f or {}).get("caption", "")).strip()
    ))
    texto_caption = "\n".join(captions)
    caption_productos = (
        _productos_de_texto_proveedor(texto_caption)
        or _productos_texto_flexible(texto_caption)
    ) if texto_caption else []
    caption_detectados = _precios_detectados(texto_caption) if texto_caption else []
    caption_solo_importe = re.fullmatch(
        r"\s*\$?\s*(\d{1,3}(?:[.,]\d{3}){1,2}|\d{4,9})\s*",
        texto_caption,
    ) if texto_caption else None
    nombre_caption = ""
    # Priorizar un producto e importe escritos explícitamente por el usuario.
    if caption_solo_importe:
        importe_caption = int(re.sub(r"\D", "", caption_solo_importe.group(1)) or "0")
        detectados = [{
            "etiqueta": "Contado", "costo": importe_caption,
            "origen": "importe escrito por el usuario junto a la foto"
        }] if 1000 <= importe_caption <= 100_000_000 else []
    elif caption_productos:
        elegido_caption = caption_productos[0]
        detectados = [{
            "etiqueta": "Contado", "costo": int(elegido_caption["costo"]),
            "origen": "texto escrito junto a la foto"
        }]
        nombre_caption = str(elegido_caption.get("producto", "")).strip()
    elif not caption_solo_importe:
        detectados = _precios_detectados(texto_ocr)
        if caption_detectados:
            efectivo_caption = [
                p for p in caption_detectados
                if p["etiqueta"].lower() in ("efectivo", "contado", "cash")
            ]
            if len(caption_detectados) == 1 or len(efectivo_caption) == 1:
                detectados = [
                    efectivo_caption[0] if len(efectivo_caption) == 1
                    else caption_detectados[0]
                ]
            elif len(caption_detectados) > 1:
                # No elegir al azar si la leyenda trae varios importes sin modalidad clara.
                detectados = []
    m_nombre_caption = re.search(r"(?im)^\s*producto\s*:\s*(.+?)\s*$", texto_caption)
    if m_nombre_caption:
        nombre_caption = m_nombre_caption.group(1).strip()[:100]
    efectivo = [p for p in detectados if p["etiqueta"].lower() in ("efectivo", "contado", "cash")]
    if len(detectados) == 1:
        detectados = [detectados[0]]
    elif len(efectivo) == 1:
        detectados = [efectivo[0]]
    elif len(detectados) > 1:
        # Respaldo automático: si el OCR perdió las etiquetas pero todos los importes
        # tienen formato monetario explícito y ninguno menciona financiación/cuotas,
        # usar el menor como precio de contado. No tomar códigos/modelos sin formato
        # monetario y no bloquear la edición del cartel por una etiqueta ilegible.
        monetarios = []
        for p in detectados:
            origen = str(p.get("origen", p.get("linea", "")))
            if (re.search(r"\$|\d{1,3}[.,]\d{3}", origen)
                    and not re.search(r"cuotas?|financiad|tarjeta|inter[eé]s|plan de pago", origen, re.I)):
                monetarios.append(p)
        if monetarios:
            elegido = min(monetarios, key=lambda p: int(p.get("costo", 0)))
            detectados = [{
                "etiqueta": "Contado",
                "costo": int(elegido["costo"]),
                "origen": "respaldo OCR: menor importe monetario explícito sin etiqueta legible"
            }]
            log.warning(
                "OCR no pudo distinguir las modalidades; se generará el borrador usando el menor importe monetario explícito: %s",
                elegido.get("costo")
            )
        else:
            log.warning(
                "Importes ambiguos sin formato monetario fiable; no se elige un precio: %s",
                [(p.get("costo"), p.get("etiqueta"), p.get("origen", "")) for p in detectados]
            )
            detectados = []
    else:
        detectados = []
    if not detectados:
        reintento_ocr_ya_realizado = bool(
            state.get("pending_manual", {}).get(str(chat_id), {}).get("auto_retry_ocr_v4")
        )
        state.setdefault("pending_manual", {})[str(chat_id)] = {
            "foto_id": foto_id, "nombres": nombres, "texto_ocr": texto_ocr[:8000],
            "precios_detectados": [], "auto_retry_ocr_v4": reintento_ocr_ya_realizado
        }
        save_state()
        enviar_texto(
            "Recibí las imágenes, pero no pude confirmar con seguridad el precio de contado. No voy a inventarlo.\n\n"
            f"Archivos: {', '.join(nombres)}\n\n"
            "Si el precio está escrito en el mensaje junto a la foto, lo usaré. Si el cartel trae varios importes sin etiquetas legibles, respondé solo con el precio de contado; no hace falta volver a escribir el nombre del producto.\n\n"
            "Los originales siguen en 1_entrada.",
            chat_id=chat_id)
        return
    porcentaje = float(GANANCIA_PORCENTAJE)
    precios = [{"etiqueta": "Contado", "costo": int(detectados[0]["costo"]),
                "venta": round(int(detectados[0]["costo"]) * (1 + porcentaje / 100))}]
    nombre = nombre_caption or pi.inferir_nombre_producto(texto_ocr)
    if nombre == "Producto":
        nombre = re.sub(r"[_-]+", " ", Path(nombres[0]).stem)[:90] or "Producto"
    cartel = pi.crear_cartel_tres_precios(foto, nombre, precios, porcentaje)
    did = uuid.uuid4().hex[:10]
    filename = f"reventa_{did}.jpg"
    upload = pi.subir_imagen_a_drive(service, cartel, filename, ID_PUBLICAR)
    rec = {"id": did, "nombre": nombre, "precios": precios, "porcentaje": porcentaje,
           "foto_drive_id": foto_id, "archivos_origen": nombres, "output_id": upload.get("id"),
           "output_name": filename, "estado": "pendiente_revision", "chat_id": str(chat_id),
           "caption": texto_cartel(nombre, precios, porcentaje), "created": int(time.time())}
    state.setdefault("drafts", {})[did] = rec
    state.get("pending_manual", {}).pop(str(chat_id), None)
    save_state()
    cartel.seek(0)
    enviar_foto(cartel, filename, rec["caption"], teclado(did), chat_id)
    log.info(
        "Borrador generado y enviado: producto=%r, costo_contado=%s, venta=%s, fotos=%d",
        nombre, precios[0]["costo"], precios[0]["venta"], len(nombres)
    )
    enviar_texto(
        f"Borrador listo para revisar.\n\nProducto: {nombre}\n"
        f"Precio de contado del proveedor: {pesos(precios[0]['costo'])}\n"
        f"Tu precio con {porcentaje:g}% de ganancia: {pesos(precios[0]['venta'])}\n\n"
        "Todavía no se publicó. Usá Publicar, Modificar o Cancelar. Los originales siguen en 1_entrada.",
        chat_id=chat_id)


def _regenerar(rec, chat_id, nuevo=False):
    service = pi.obtener_servicio_drive()
    foto_id = rec.get("foto_drive_id")
    foto = pi.descargar_imagen_a_memoria(service, foto_id) if foto_id else None
    cartel = pi.crear_cartel_tres_precios(foto, rec["nombre"], rec["precios"], rec["porcentaje"])
    filename = f"reventa_{rec['id']}_{uuid.uuid4().hex[:5]}.jpg"
    upload = pi.subir_imagen_a_drive(service, cartel, filename, ID_PUBLICAR)
    rec["output_id"] = upload.get("id"); rec["output_name"] = filename
    rec["caption"] = texto_cartel(rec["nombre"], rec["precios"], rec["porcentaje"])
    rec["estado"] = "pendiente_revision"; save_state(); cartel.seek(0)
    enviar_foto(cartel, filename, rec["caption"], teclado(rec["id"]), chat_id)
    if nuevo:
        enviar_texto("BORRADOR LISTO PARA REVISAR. La imagen está preparada, todavía no se publicó en Facebook. Usá Publicar, Modificar o Cancelar en los botones del cartel.", chat_id=chat_id)
    else:
        enviar_texto("Cartel actualizado. Revisá la imagen nueva y volvé a confirmar, modificar o cancelar.", chat_id=chat_id)


def _modificar(rec, chat_id, instruccion):
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*%", instruccion)
    if m:
        pct = float(m.group(1).replace(",", "."))
        if not 0 <= pct <= 500:
            enviar_texto("El porcentaje debe estar entre 0 y 500.", chat_id=chat_id); return
        rec["porcentaje"] = pct
        for p in rec["precios"]: p["venta"] = round(p["costo"] * (1 + pct / 100))
    else:
        # Para promociones de varios productos, permitir corregir el precio final nombrando el producto.
        importe_final = re.search(r"(\d[\d.,]*)\s*$", instruccion.strip())
        if importe_final:
            importe = int(re.sub(r"\D", "", importe_final.group(1)) or "0")
            texto_producto = instruccion[:importe_final.start()].strip(" :=$-")
            coincidencia = next(
                (p for p in rec["precios"] if p.get("producto") and
                 (p["producto"].lower() in texto_producto.lower() or
                  texto_producto.lower() in p["producto"].lower())),
                None
            )
            if coincidencia and importe > 0:
                coincidencia["venta"] = importe
                _regenerar(rec, chat_id)
                return
        m = re.search(r"(lista|efectivo|contado|cash|transferencia|precio\s*[1-3]|mayorista|minorista)\D{0,20}(\d[\d.,]*)", instruccion, re.I)
        if not m:
            enviar_texto("No entendí el cambio. Escribí 35% para recalcular todo, o por ejemplo: efectivo 125000.", chat_id=chat_id)
            return
        modalidad = re.sub(r"\s+", " ", m.group(1).strip()).title()
        if modalidad.lower() in ("efectivo", "contado", "cash"): modalidad = "Contado"
        valor = int(re.sub(r"\D", "", m.group(2)))
        if valor <= 0: enviar_texto("El precio debe ser mayor que cero.", chat_id=chat_id); return
        item = next((p for p in rec["precios"] if p["etiqueta"].lower() == modalidad.lower()), None)
        if not item: enviar_texto("No encontré esa modalidad en este cartel. Usá una etiqueta que aparezca en la imagen, por ejemplo Lista, Efectivo, Transferencia o Precio 1.", chat_id=chat_id); return
        item["venta"] = valor
    _regenerar(rec, chat_id)


def _editar_caption(chat_id, message_id, caption):
    try:
        api("editMessageCaption", {"chat_id": str(chat_id), "message_id": message_id, "caption": caption[:1024],
            "reply_markup": json.dumps({"inline_keyboard": []}, ensure_ascii=False)})
    except Exception: log.exception("No pude editar el mensaje de Telegram")


def procesar_callback(cb):
    try: api("answerCallbackQuery", {"callback_query_id": cb.get("id"), "text": "Procesando"})
    except Exception: pass
    data = cb.get("data", "").split(":"); msg = cb.get("message", {})
    if len(data) < 2: return
    action, did = data[0], data[1]; chat_id = str(msg.get("chat", {}).get("id", TELEGRAM_CHAT_ID))
    rec = state.get("drafts", {}).get(did)
    if not rec: enviar_texto("No encuentro ese borrador. Volvé a subir las imágenes a Drive.", chat_id=chat_id); return
    if action == "ok":
        if rec.get("estado") == "publicado_facebook":
            enviar_texto("Esta promoción ya figura como publicada en Facebook. No se volvió a publicar.", chat_id=chat_id)
            return
        if rec.get("estado") == "publicando_facebook":
            enviar_texto("La publicación ya está en curso. Esperá el resultado antes de volver a intentarlo.", chat_id=chat_id)
            return
        rec["estado"] = "publicando_facebook"
        rec["approved_at"] = rec.get("approved_at") or int(time.time())
        save_state()
        try:
            post_id = publicar_en_facebook(rec)
            rec["estado"] = "publicado_facebook"
            rec["facebook_post_id"] = post_id
            rec["published_at"] = int(time.time())
            rec.pop("error_publicacion", None)
            save_state()
            _editar_caption(chat_id, msg.get("message_id"), "✅ PUBLICADO EN FACEBOOK\n\n" + rec.get("caption", ""))
            enviar_texto(
                "✅ La promoción se publicó correctamente en la página de Facebook.\n"
                + (f"ID de publicación: {post_id}\n" if post_id else "")
                + "Los originales permanecen en 1_entrada.",
                chat_id=chat_id,
            )
        except Exception as e:
            rec["estado"] = "error_publicacion_facebook"
            rec["error_publicacion"] = str(e)[:500]
            save_state()
            log.exception("No se pudo publicar la promoción aprobada en Facebook")
            enviar_texto(
                "⚠️ El cartel fue aprobado, pero Facebook no confirmó la publicación. "
                "No se marcará como publicado. Detalle: " + str(e)[:700]
                + "\nCorregí la configuración indicada y volvé a tocar «Confirmar cartel» para reintentar.",
                chat_id=chat_id,
            )
    elif action == "edit":
        state.setdefault("waiting_edit", {})[chat_id] = did; save_state()
        enviar_texto("Escribí un porcentaje nuevo (ejemplo: 35%) para recalcular todos los precios, o un precio final específico (ejemplo: efectivo 125000).", chat_id=chat_id)
    elif action == "cancel":
        rec["estado"] = "cancelado"; save_state()
        _editar_caption(chat_id, msg.get("message_id"), "❌ BORRADOR CANCELADO\nLos originales siguen en 1_entrada.")
        enviar_texto("Borrador cancelado. No se publicó nada ni se movieron los originales.", chat_id=chat_id)


def _guardar_imagen_telegram_en_drive(msg, chat_id):
    """Descarga la imagen de Telegram, la guarda en 1_entrada y la registra como pendiente."""
    foto = None
    nombre = None
    mime_type = "image/jpeg"
    if msg.get("photo"):
        foto = msg["photo"][-1]
        nombre = f"telegram_{int(time.time())}_{foto.get('file_unique_id', uuid.uuid4().hex)}.jpg"
    else:
        documento = msg.get("document") or {}
        mime = str(documento.get("mime_type", "")).lower()
        if not mime.startswith("image/"):
            return None
        foto = documento
        extension = Path(str(documento.get("file_name") or "imagen.jpg")).suffix.lower()
        if extension not in (".jpg", ".jpeg", ".png", ".webp"):
            extension = ".jpg" if mime in ("image/jpeg", "image/jpg") else ".png" if mime == "image/png" else ".webp" if mime == "image/webp" else ".jpg"
        nombre = f"telegram_{int(time.time())}_{documento.get('file_unique_id', uuid.uuid4().hex)}{extension}"
        mime_type = mime or "image/jpeg"
    if not foto:
        return None
    file_id = foto.get("file_id")
    if not file_id:
        raise RuntimeError("Telegram no entregó el identificador de la imagen.")
    meta = requests.get(f"{API}/getFile", params={"file_id": file_id}, timeout=30)
    meta.raise_for_status()
    payload = meta.json()
    if not payload.get("ok") or not payload.get("result", {}).get("file_path"):
        raise RuntimeError(payload.get("description", "Telegram no permitió descargar la imagen."))
    file_url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{payload['result']['file_path']}"
    descarga = requests.get(file_url, timeout=90)
    descarga.raise_for_status()
    if not descarga.content:
        raise RuntimeError("Telegram devolvió una imagen vacía.")
    buffer = io.BytesIO(descarga.content)
    service = pi.obtener_servicio_drive()
    subida = pi.subir_imagen_a_drive(service, buffer, nombre, ID_ENTRADA, mime_type=mime_type)
    texto_adjunto = str(msg.get("caption") or msg.get("text") or "").strip()
    file_meta = {"id": subida["id"], "name": subida.get("name", nombre), "mimeType": mime_type,
                 "caption": texto_adjunto}
    ahora = time.time()
    # Registrar el archivo en el estado de trabajo de inmediato: el procesamiento no depende
    # de que otro escaneo llegue a descubrirlo en Drive.
    state.setdefault("pending_drive", {})[file_meta["id"]] = {
        "file": file_meta, "first_seen": ahora, "last_seen": ahora,
        "attempts": 0, "next_retry": 0
    }
    seen = state.setdefault("seen_drive_ids", [])
    if file_meta["id"] not in seen:
        seen.append(file_meta["id"])
    state["seen_drive_ids"] = seen[-1500:]
    save_state()
    log.info("Foto recibida por Telegram guardada y registrada para procesar: %s (%s)", nombre, file_meta["id"])
    return {"id": file_meta["id"], "name": nombre}

def procesar_mensaje(msg):
    chat_id = str(msg.get("chat", {}).get("id", ""))
    if chat_id != str(TELEGRAM_CHAT_ID):
        log.warning("Mensaje ignorado: chat de Telegram no autorizado (chat_id=%s). Revisar PUBLISHER_TELEGRAM_CHAT_ID en GitHub Actions.", chat_id or "vacío")
        if (msg.get("text") or "").strip().lower() in ("/start", "/ayuda", "ayuda"):
            enviar_texto("El bot está en línea, pero este chat no está autorizado para operar. Revisá PUBLISHER_TELEGRAM_CHAT_ID en los secretos de GitHub Actions.", chat_id=chat_id)
        return
    # Las fotos se guardan en Drive. Si vienen con texto/caption, se procesa junto con la foto.
    texto = (msg.get("text") or msg.get("caption") or "").strip()
    es_imagen = bool(msg.get("photo") or (msg.get("document") or {}).get("mime_type", "").lower().startswith("image/"))

    # Telegram suele mandar la foto y el nombre/precio como dos mensajes separados.
    # Si hay fotos recientes esperando análisis, asociarles el texto para que no se
    # procese como una oferta distinta sin fotografía.
    if texto and not es_imagen and chat_id not in state.get("waiting_edit", {}) and not state.get("pending_manual", {}).get(chat_id):
        parece_datos_producto = bool(
            _productos_de_texto_proveedor(texto)
            or _productos_texto_flexible(texto)
            or re.search(
                r"(?is)(?:producto\s*:|precio\s*:|efectivo\s*:|contado\s*:|transferencia\s*:|lista\s*:|\$\s*\d|\d{1,3}[.,]\d{3}|^\s*\d{4,}(?:[.,]\d{3})*\s*$)",
                texto,
            )
        )
        if parece_datos_producto:
            ahora_adjuntar = time.time()
            cola = state.setdefault("pending_drive", {})
            recientes = [
                (fid, item) for fid, item in cola.items()
                if ahora_adjuntar - float(item.get("last_seen", item.get("first_seen", 0))) <= 900
                and isinstance(item.get("file"), dict)
            ]
            recientes.sort(key=lambda par: float(par[1].get("first_seen", par[1].get("last_seen", 0))))
            if recientes:
                grupo = recientes[-2:]
                for fid, item in grupo:
                    item["file"]["caption"] = texto
                    item["last_seen"] = ahora_adjuntar
                save_state()
                log.info(
                    "Texto de producto asociado a %s foto(s) pendientes; no se tratará como oferta separada.",
                    len(grupo),
                )
                enviar_texto(
                    "Asocié el nombre/precio que enviaste con las fotos pendientes. Voy a usar ese dato para preparar el cartel y no crear una oferta aparte.",
                    chat_id=chat_id,
                )
                return

    if es_imagen:
        try:
            foto_recibida = _guardar_imagen_telegram_en_drive(msg, chat_id)
            if foto_recibida:
                # Toda foto queda en la cola de Drive para que el analizador de imágenes
                # pueda asociar la foto limpia con la lista de precios. No pasar el caption
                # al parser de texto: eso provocaba el falso mensaje "No pude leer ningún importe".
                save_state()
                if texto:
                    log.info("Foto con texto adjunto conservada en la cola de imágenes: %s", foto_recibida["name"])
                    enviar_texto(
                        f"Foto recibida y guardada en 1_entrada de Google Drive: {foto_recibida['name']}.\n"
                        "El texto adjunto no se procesará como un mensaje separado. Enviá la otra foto del mismo producto; analizaré el par y te enviaré un borrador para revisar antes de publicar.",
                        chat_id=chat_id)
                else:
                    enviar_texto(
                        f"Foto recibida y guardada en 1_entrada de Google Drive: {foto_recibida['name']}.\n"
                        "Quedó registrada para procesar. Enviá la otra foto ahora; cuando termines, el sistema analizará el lote y te enviará el borrador con botones para revisar antes de publicar.",
                        chat_id=chat_id)
                return
        except Exception as e:
            log.exception("No pude guardar en Drive una imagen recibida por Telegram")
            enviar_texto(f"No pude guardar la foto en 1_entrada de Google Drive: {e}. No se inició el análisis; podés volver a enviarla.", chat_id=chat_id)
            return
    if not texto:
        return
    if texto.lower() in ("/start", "/ayuda", "ayuda"):
        enviar_texto("Revendedor WA activo. Enviá directamente por este chat las fotos del producto y de la lista de precios: las guardaré en 1_entrada de Google Drive y comenzaré el análisis automáticamente. También podés enviar un cartel sin foto con:\n\nProducto: Taladro\nPrecio: 125000\n\nO usar Efectivo y Transferencia. No uses precios financiados ni cuotas. Los originales no se mueven y nada se publica en Facebook sin tu confirmación.", chat_id=chat_id); return
    if chat_id in state.get("waiting_edit", {}):
        did = state["waiting_edit"].pop(chat_id, None); save_state(); rec = state.get("drafts", {}).get(did)
        if not rec: enviar_texto("No encuentro el borrador para modificar.", chat_id=chat_id); return
        _modificar(rec, chat_id, texto); return
    # Acepta precios manuales asociados a una foto pendiente o un cartel creado desde cero.
    pendiente = state.get("pending_manual", {}).get(chat_id, {})
    sin_foto = not bool(pendiente)
    # Si se pidió aclaración y se responde con un número solo, tomarlo como precio contado.
    if pendiente and re.fullmatch(r"\s*\$?\s*\d[\d.,:'’´\s]*\s*", texto):
        importe = int(re.sub(r"\D", "", texto) or "0")
        if 1000 <= importe <= 100_000_000:
            nombre_pendiente = pi.inferir_nombre_producto(pendiente.get("texto_ocr", ""))
            texto = f"Producto: {nombre_pendiente}\nEfectivo: {importe}"
    productos_proveedor = _productos_de_texto_proveedor(texto)
    if not productos_proveedor:
        productos_proveedor = _productos_texto_flexible(texto)
    log.info(
        "Mensaje de Telegram recibido: chat_autorizado=%s, texto=%s, caracteres=%d, fotos=%d, productos_detectados=%d",
        chat_id == str(TELEGRAM_CHAT_ID), bool(texto), len(texto),
        len(msg.get("photo", [])), len(productos_proveedor),
    )
    if sin_foto:
        tiene_producto = re.search(r"(?im)^\s*producto\s*:\s*.+$", texto)
        tiene_precio = re.search(
            r"(?im)^\s*(precio|contado|efectivo|transferencia|lista)\s*:\s*\$?\s*[\d.,]+\s*$",
            texto,
        )
        if not productos_proveedor and not (tiene_producto and tiene_precio):
            enviar_texto(
                "También podés crear un cartel sin foto. Enviá este formato:\n\n"
                "Producto: Taladro\nPrecio: 125000\n\n"
                "O indicá el precio de contado/transferencia:\n"
                "Producto: Amoladora\nEfectivo: 45000\nTransferencia: 47000\n\n"
                "No uses precios financiados ni cuotas.",
                chat_id=chat_id,
            )
            return
    nombre_match = re.search(r"(?im)^\s*producto\s*:\s*(.+?)\s*$", texto)
    if productos_proveedor:
        nombre = "Ofertas del día" if len(productos_proveedor) > 1 else productos_proveedor[0]["producto"]
        detectados = productos_proveedor
    else:
        nombre = nombre_match.group(1).strip()[:100] if nombre_match else pi.inferir_nombre_producto(texto)
        detectados = _precios_detectados(texto)
    # El formato manual puede usar nombres propios como Precio, Lista, Efectivo o Transferencia.
    manuales = []
    for linea in texto.splitlines():
        m = re.match(r"\s*([A-Za-zÁÉÍÓÚÜÑáéíóúüñ ]{2,24})\s*:\s*\$?\s*([\d.,]+)\s*$", linea)
        if not m or m.group(1).strip().lower() == "producto":
            continue
        etiqueta_raw = re.sub(r"\s+", " ", m.group(1).strip())
        if etiqueta_raw.lower() not in {"precio", "contado", "efectivo", "transferencia", "lista"}:
            continue
        etiqueta = "Efectivo" if etiqueta_raw.lower() == "contado" else etiqueta_raw.title()
        valor = int(re.sub(r"\D", "", m.group(2)) or "0")
        if valor > 0:
            manuales.append({"etiqueta": etiqueta, "costo": valor})
    if manuales:
        detectados = manuales[:3]
    if not detectados:
        enviar_texto(
            "No pude leer ningún importe. Enviá entre 1 y 3 líneas de precios, por ejemplo:\n"
            "Producto: Taladro\nPrecio: 125000\n\nTambién podés usar Lista: 125000, Efectivo: 115000 y Transferencia: 120000.",
            chat_id=chat_id)
        return
    porcentaje = float(GANANCIA_PORCENTAJE)
    if productos_proveedor and len(productos_proveedor) > 6:
        grupos = [productos_proveedor[i:i + 6] for i in range(0, len(productos_proveedor), 6)]
        ids_creados = []
        for indice, grupo in enumerate(grupos, start=1):
            precios_grupo = [{
                "etiqueta": "Contado", "producto": p["producto"],
                "moneda": p.get("moneda", "ARS"), "costo": int(p["costo"]),
                "venta": round(int(p["costo"]) * (1 + porcentaje / 100))
            } for p in grupo]
            nombre_grupo = "Ofertas del día" if len(grupos) == 1 else f"Ofertas del día ({indice}/{len(grupos)})"
            rec_grupo = {
                "id": uuid.uuid4().hex[:10], "nombre": nombre_grupo,
                "precios": precios_grupo, "porcentaje": porcentaje, "foto_drive_id": None,
                "archivos_origen": [], "estado": "pendiente_revision",
                "chat_id": chat_id, "created": int(time.time())
            }
            state.setdefault("drafts", {})[rec_grupo["id"]] = rec_grupo
            ids_creados.append(rec_grupo["id"])
        save_state()
        for did_grupo in ids_creados:
            _regenerar(state["drafts"][did_grupo], chat_id, nuevo=True)
        return
    precios = [{
        "etiqueta": p["etiqueta"],
        "producto": p.get("producto", ""),
        "moneda": p.get("moneda", "ARS"),
        "costo": int(p["costo"]),
        "venta": round(int(p["costo"]) * (1 + porcentaje / 100))
    } for p in detectados[:6]]
    foto_id = pendiente.get("foto_id")
    if not foto_id and not sin_foto:
        enviar_texto("No encuentro la foto pendiente asociada. Subí las imágenes a 1_entrada y esperá el análisis.", chat_id=chat_id)
        return
    rec = {"id": uuid.uuid4().hex[:10], "nombre": nombre if nombre != "Producto" else "Producto",
           "precios": precios, "porcentaje": porcentaje, "foto_drive_id": foto_id,
           "archivos_origen": pendiente.get("nombres", []), "estado": "pendiente_revision",
           "chat_id": chat_id, "created": int(time.time())}
    state.setdefault("drafts", {})[rec["id"]] = rec
    save_state()
    try:
        _regenerar(rec, chat_id, nuevo=True)
        if not sin_foto:
            state.get("pending_manual", {}).pop(chat_id, None)
        save_state()
    except Exception as e:
        log.exception("No pude crear cartel con precios manuales")
        enviar_texto(f"Error al crear el cartel: {e}. La foto pendiente se conserva para volver a intentarlo.", chat_id=chat_id)


def vigilar_drive_una_vez():
    """Una revisión de Drive: se ejecuta una vez por tarea en la nube."""
    log.info("Revisión de Drive iniciada; no mueve ni borra originales.")
    service = pi.obtener_servicio_drive()
    files = [x for x in pi.listar_archivos_en_carpeta(service, ID_ENTRADA) if pi.es_imagen(x)]
    seen = set(state.get("seen_drive_ids", []))
    now = time.time()
    pending = state.setdefault("pending_drive", {})
    for f in files:
        fid = f["id"]
        if fid not in seen:
            pending.setdefault(fid, {"file": f, "first_seen": now, "last_seen": now, "attempts": 0, "next_retry": 0})
            seen.add(fid)
    # Una sola relectura automática de lotes que el OCR anterior dejó pendientes manuales.
    # Permite que la corrección del OCR procese las fotos ya recibidas sin pedir que se reenvíen.
    for chat_manual, manual in state.get("pending_manual", {}).items():
        # Versión v4: recuperar fotos de lotes antiguos aunque hayan quedado marcadas
        # como reintentadas por versiones previas. No repetir después de esta recuperación.
        if manual.get("auto_retry_ocr_v4"):
            continue
        nombres_manual = set(manual.get("nombres", []))
        coincidentes = [f for f in files if f.get("name") in nombres_manual]
        if coincidentes:
            for f in coincidentes:
                pending.setdefault(f["id"], {"file": f, "first_seen": now, "last_seen": now, "attempts": 0, "next_retry": 0})
            manual["auto_retry_ocr_v4"] = True
            log.info("Recuperación v4: reencolando %s foto(s) del lote pendiente de %s.", len(coincidentes), chat_manual)
    state["seen_drive_ids"] = list(seen)[-1500:]
    save_state()
    if not pending:
        log.info("No hay imágenes pendientes en 1_entrada.")
        return
    latest = max(x.get("last_seen", x.get("first_seen", now)) for x in pending.values())
    # Dar tiempo a que termine la carga de todas las fotos de un lote.
    # El escaneo de Drive ocurre cada 45 s: esperar solo 30 s hacía que la primera foto
    # se procesara sola antes de que el siguiente escaneo detectara la segunda. Dar una
    # ventana real para recibir el par (folleto de precios + foto limpia).
    ventana_lote = max(90, int(os.environ.get("CLOUD_BATCH_WAIT_SECONDS", "90")))
    if now - latest < ventana_lote:
        log.info("Hay imágenes recientes; espero %s s desde la última detección para agrupar las fotos del producto.", ventana_lote)
        return
    # Cada promoción se arma con un par de imágenes: primero la lista de precios y
    # luego la foto limpia (o viceversa). No mezclar dos productos enviados seguidos.
    def _ordenar_foto_lote(fid):
        archivo = pending[fid].get("file", {})
        nombre_archivo = str(archivo.get("name", ""))
        # Los archivos guardados por Telegram incluyen la hora Unix en el nombre.
        # Usarla evita que el orden aleatorio de Drive mezcle las parejas antiguas.
        m_hora = re.match(r"telegram_(\d+)_", nombre_archivo)
        if m_hora:
            return (0, int(m_hora.group(1)))
        return (1, float(pending[fid].get("first_seen", pending[fid].get("last_seen", now))))

    lote_ids = sorted(pending.keys(), key=_ordenar_foto_lote)[:2]
    lote = [pending[fid]["file"] for fid in lote_ids if "file" in pending[fid]]
    if not lote or not all(now >= pending[fid].get("next_retry", 0) for fid in lote_ids if fid in pending):
        log.info("Hay un lote en espera de reintento; no corresponde procesarlo en este ciclo.")
        return
    try:
        _crear_borrador(service, lote, TELEGRAM_CHAT_ID)
        for fid in lote_ids:
            pending.pop(fid, None)
        state.setdefault("processed_drive_ids", []).extend(lote_ids)
        state["processed_drive_ids"] = list(dict.fromkeys(state["processed_drive_ids"]))[-3000:]
        save_state()
        log.info("Lote de Drive procesado y estado persistido.")
    except Exception as e:
        log.exception("Error procesando lote de Drive; se conserva para reintento")
        for fid in lote_ids:
            if fid in pending:
                pending[fid]["attempts"] = int(pending[fid].get("attempts", 0)) + 1
                espera = min(300, 20 * (2 ** min(pending[fid]["attempts"], 4)))
                pending[fid]["next_retry"] = now + espera
        save_state()
        try:
            enviar_texto(f"No pude terminar el lote: {e}. Conservé las imágenes pendientes y volveré a intentarlo. Los originales siguen en 1_entrada.")
        except Exception:
            log.exception("Tampoco pude notificar el error por Telegram")
        raise


def vigilar_drive():
    log.info("Monitor local de Drive activo.")
    while True:
        try:
            vigilar_drive_una_vez()
        except Exception:
            log.exception("Error revisando Drive")
        time.sleep(max(10, int(ESCANEO_DRIVE_SEGUNDOS)))


def escuchar_botones():
    me = api("getMe"); log.info("Bot conectado: @%s", me.get("username", "sin_usuario"))
    while True:
        try:
            r = requests.get(f"{API}/getUpdates", params={"offset": state.get("last_update_id", 0)+1, "timeout": 25,
                "allowed_updates": json.dumps(["message", "callback_query"])}, timeout=35).json()
            if not r.get("ok"): raise RuntimeError(r.get("description", "getUpdates falló"))
            for upd in r.get("result", []):
                state["last_update_id"] = upd["update_id"]
                if "callback_query" in upd: procesar_callback(upd["callback_query"])
                elif "message" in upd: procesar_mensaje(upd["message"])
                save_state()
        except Exception: log.exception("Error en Telegram"); time.sleep(3)


def escuchar_botones_una_vez(timeout=0):
    """Consume actualizaciones de Telegram; timeout>0 usa long polling para responder más rápido."""
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("Falta PUBLISHER_TELEGRAM_BOT_TOKEN en los secretos de ejecución.")
    timeout = max(0, min(25, int(timeout)))
    offset = int(state.get("last_update_id", 0)) + 1
    params = {"offset": offset, "timeout": timeout,
              "allowed_updates": json.dumps(["message", "callback_query"])}
    payload = None
    ultimo_error = None
    for intento in range(5):
        try:
            response = requests.get(f"{API}/getUpdates", params=params, timeout=max(15, timeout + 10))
            if response.status_code == 429 or response.status_code in (500, 502, 503, 504):
                espera = min(30, 2 ** intento)
                try:
                    detalle = response.json()
                    espera = min(60, max(espera, int(detalle.get("parameters", {}).get("retry_after", 0))))
                except Exception:
                    pass
                ultimo_error = RuntimeError(f"getUpdates HTTP {response.status_code}")
                if intento < 4:
                    log.warning("Telegram getUpdates devolvió HTTP %s; reintento en %ss (%s/5).", response.status_code, espera, intento + 1)
                    time.sleep(espera)
                    continue
                response.raise_for_status()
            response.raise_for_status()
            payload = response.json()
            if not payload.get("ok"):
                if payload.get("error_code") == 429 and intento < 4:
                    espera = min(60, int(payload.get("parameters", {}).get("retry_after", 2 ** intento)))
                    log.warning("Telegram limitó getUpdates; reintento en %ss.", espera)
                    time.sleep(espera)
                    continue
                raise RuntimeError(payload.get("description", "getUpdates falló"))
            break
        except (requests.RequestException, ValueError) as exc:
            ultimo_error = exc
            if intento >= 4:
                raise
            espera = min(30, 2 ** intento)
            log.warning("Error temporal consultando Telegram (%s/5): %s. Reintento en %ss.", intento + 1, exc, espera)
            time.sleep(espera)
    if payload is None:
        raise RuntimeError(f"Telegram getUpdates falló tras 5 intentos: {ultimo_error}")
    updates = payload.get("result", [])
    for upd in updates:
        state["last_update_id"] = int(upd["update_id"])
        if "callback_query" in upd:
            procesar_callback(upd["callback_query"])
        elif "message" in upd:
            procesar_mensaje(upd["message"])
        save_state()
    if updates:
        log.info("Telegram: %s actualización(es) procesada(s).", len(updates))
    return len(updates)


def ejecutar_ciclo_cloud():
    """Una pasada por ejecución: GitHub Actions vuelve a invocar cada cinco minutos."""
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("Falta PUBLISHER_TELEGRAM_BOT_TOKEN en los secretos del repositorio.")
    if not TELEGRAM_CHAT_ID or not ID_ENTRADA or not ID_PUBLICAR:
        raise RuntimeError("Faltan TELEGRAM_CHAT_ID o IDs de carpetas de Drive en la configuración.")
    load_state()
    log.info("Publicador en nube iniciado: una pasada de Telegram y una revisión de Drive.")
    try:
        escuchar_botones_una_vez(timeout=20)
    except Exception:
        log.exception("Falló la consulta de Telegram; se continuará con la revisión de Drive en la próxima ejecución.")
    try:
        vigilar_drive_una_vez()
    except Exception:
        log.exception("Falló la revisión de Drive; se conservarán los elementos pendientes para reintento.")
    save_state()
    log.info("Pasada terminada. La próxima ejecución programada volverá a consultar Telegram y Drive.")

def main():
    if not TELEGRAM_BOT_TOKEN or "PEGAR" in TELEGRAM_BOT_TOKEN: raise RuntimeError("Falta TELEGRAM_BOT_TOKEN en la configuración; no compartas el token.")
    load_state(); threading.Thread(target=vigilar_drive, daemon=True).start(); escuchar_botones()

if __name__ == "__main__": main()
