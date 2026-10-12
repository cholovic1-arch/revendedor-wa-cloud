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
    r = requests.post(f"{API}/{method}", data=data or {}, files=files, timeout=timeout); r.raise_for_status()
    j = r.json()
    if not j.get("ok"): raise RuntimeError(j.get("description", f"Telegram {method} falló"))
    return j.get("result")


def enviar_texto(texto, reply_markup=None, chat_id=None):
    data = {"chat_id": str(chat_id or TELEGRAM_CHAT_ID), "text": texto}
    if reply_markup: data["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    return api("sendMessage", data=data)


def enviar_foto(buffer, nombre, caption, keyboard=None, chat_id=None):
    buffer.seek(0); data = {"chat_id": str(chat_id or TELEGRAM_CHAT_ID), "caption": caption[:1024]}
    if keyboard: data["reply_markup"] = json.dumps(keyboard, ensure_ascii=False)
    return api("sendPhoto", data=data, files={"photo": (nombre, buffer, "image/jpeg")}, timeout=90)


def pesos(n): return "$" + f"{int(round(float(n))):,}".replace(",", ".")


def texto_cartel(nombre, precios, porcentaje):
    """Texto promocional para clientes; nunca revela el margen interno del revendedor."""
    filas = "\n".join(f"• {p['etiqueta']}: {pesos(p['venta'])}" for p in precios)
    return (
        f"🔥 ¡OFERTA PARA APROVECHAR! 🔥\n\n"
        f"🛍️ {nombre}\n\n"
        f"💰 PRECIOS ESPECIALES:\n{filas}\n\n"
        "📩 Escribinos por mensaje privado para consultar o reservar.\n"
        "📦 Consultá disponibilidad."
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
        # Nunca usar automáticamente importes de cuotas o financiación.
        linea = str(p.get("linea", ""))
        if re.search(r"financiad|cuotas?|tarjeta de cr[eé]dito|plan de pago|inter[eé]s", linea, re.I):
            continue
        try:
            valor = int(p.get("costo", 0))
        except (TypeError, ValueError):
            continue
        if valor <= 0 or valor in vistos:
            continue
        etiqueta = str(p.get("etiqueta", "")).strip()
        if etiqueta.lower() in ("", "sin identificar", "desconocido", "none"):
            etiqueta = f"Precio {len(resultado) + 1}"
        resultado.append({"etiqueta": etiqueta, "costo": valor, "origen": p.get("linea", "")})
        vistos.add(valor)
        if len(resultado) == 3:
            break
    return resultado


def _precios_etiquetados(texto):
    """Compatibilidad: devuelve modalidades explícitas reconocidas por OCR."""
    mapa = {}
    for p in pi.extraer_precios(texto):
        if p["etiqueta"] in ("Lista", "Efectivo", "Transferencia"):
            mapa.setdefault(p["etiqueta"], p["costo"])
    return mapa


def _foto_y_texto(service, lote):
    """Clasifica heurísticamente foto limpia vs. folleto/lista; si OCR es ambiguo no inventa importes."""
    items = []
    for f in lote:
        buff = pi.descargar_imagen_a_memoria(service, f["id"])
        ocr = pi.leer_texto_ocr(buff)
        p = pi.extraer_precios(ocr)
        etiquetas = {x["etiqueta"] for x in p}
        señales = sum(1 for k in ("precio", "efectivo", "transfer", "lista", "mayorista", "minorista") if k in ocr.lower())
        score = señales + len(p) * 2 + len(etiquetas & {"Lista", "Efectivo", "Transferencia"}) * 4
        items.append({"file": f, "ocr": ocr, "score": score, "buff": pi.descargar_imagen_a_memoria(service, f["id"])})
    if not items: raise ValueError("No hay imágenes para procesar")
    # La imagen con menos texto/precios se considera candidata a foto del producto.
    items.sort(key=lambda x: x["score"])
    foto = items[0]
    # Combina el OCR de todas las imágenes, ya que algunos proveedores separan título y precios.
    texto = "\n\n".join(x["ocr"] for x in sorted(items, key=lambda x: x["score"], reverse=True))
    nombres = [x["file"].get("name", "imagen") for x in items]
    return foto["buff"], foto["file"]["id"], nombres, texto


def _crear_borrador(service, lote, chat_id):
    foto, foto_id, nombres, texto_ocr = _foto_y_texto(service, lote)
    detectados = _precios_detectados(texto_ocr)
    if not detectados:
        state.setdefault("pending_manual", {})[str(chat_id)] = {
            "foto_id": foto_id, "nombres": nombres, "texto_ocr": texto_ocr[:8000],
            "precios_detectados": []
        }
        save_state()
        enviar_texto(
            "Detecté las imágenes, pero no pude leer ningún precio con suficiente seguridad. No voy a inventar importes.\n\n"
            f"Archivos: {', '.join(nombres)}\n\nTexto reconocido:\n{(texto_ocr or 'No se reconoció texto')[:1800]}\n\n"
            "No pude confirmar un importe numérico. No hace falta que existan tres modalidades: se acepta uno, dos o tres precios.\n\n"
            "Si querés completar solo este lote, respondé por ejemplo:\n"
            "Producto: Parrilla desarmable\nLista: 174000\nEfectivo: 145000\n\n"
            "Los originales no se mueven.",
            chat_id=chat_id)
        return
    porcentaje = float(GANANCIA_PORCENTAJE)
    precios = [{"etiqueta": p["etiqueta"], "costo": p["costo"],
                "venta": round(p["costo"] * (1 + porcentaje / 100))} for p in detectados[:3]]
    nombre = pi.inferir_nombre_producto(texto_ocr)
    if nombre == "Producto":
        # Elegir el nombre del archivo que parece más descriptivo, no asumir siempre el primero.
        nombre = re.sub(r"[_-]+", " ", Path(nombres[0]).stem)[:90] or "Producto"
    cartel = pi.crear_cartel_tres_precios(foto, nombre, precios, porcentaje)
    did = uuid.uuid4().hex[:10]; filename = f"reventa_{did}.jpg"
    upload = pi.subir_imagen_a_drive(service, cartel, filename, ID_PUBLICAR)
    rec = {"id": did, "nombre": nombre, "precios": precios, "porcentaje": porcentaje,
           "foto_drive_id": foto_id, "archivos_origen": nombres, "output_id": upload.get("id"),
           "output_name": filename, "estado": "pendiente", "chat_id": str(chat_id),
           "caption": texto_cartel(nombre, precios, porcentaje), "created": int(time.time())}
    state.setdefault("drafts", {})[did] = rec; save_state(); cartel.seek(0)
    enviar_foto(cartel, filename, rec["caption"], teclado(did), chat_id)
    resumen = "\n".join(f"• {p['etiqueta']}: costo {pesos(p['costo'])} → reventa {pesos(p['venta'])}" for p in precios)
    enviar_texto(
        f"Borrador generado con {len(precios)} precio(s), guardado en la carpeta de salida de Drive.\n\n{resumen}\n\n"
        f"Margen aplicado: {porcentaje:g}%. Revisá los importes. No se publicó externamente y los originales siguen en 1_entrada.",
        chat_id=chat_id)


def _regenerar(rec, chat_id):
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
        m = re.search(r"(lista|efectivo|transferencia|precio\s*[1-3]|mayorista|minorista)\D{0,20}(\d[\d.,]*)", instruccion, re.I)
        if not m:
            enviar_texto("No entendí el cambio. Escribí 35% para recalcular todo, o por ejemplo: efectivo 125000.", chat_id=chat_id)
            return
        modalidad = re.sub(r"\s+", " ", m.group(1).strip()).title(); valor = int(re.sub(r"\D", "", m.group(2)))
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
    file_meta = {"id": subida["id"], "name": subida.get("name", nombre), "mimeType": mime_type}
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
    return nombre

def procesar_mensaje(msg):
    chat_id = str(msg.get("chat", {}).get("id", ""))
    if chat_id != str(TELEGRAM_CHAT_ID):
        log.warning("Mensaje ignorado: chat de Telegram no autorizado (chat_id=%s). Revisar PUBLISHER_TELEGRAM_CHAT_ID en GitHub Actions.", chat_id or "vacío")
        if (msg.get("text") or "").strip().lower() in ("/start", "/ayuda", "ayuda"):
            enviar_texto("El bot está en línea, pero este chat no está autorizado para operar. Revisá PUBLISHER_TELEGRAM_CHAT_ID en los secretos de GitHub Actions.", chat_id=chat_id)
        return
    # Las fotos se reciben directamente por Telegram, se guardan en 1_entrada de Drive
    # y el ciclo normal las detecta para iniciar el análisis del lote.
    if msg.get("photo") or (msg.get("document") or {}).get("mime_type", "").lower().startswith("image/"):
        try:
            nombre = _guardar_imagen_telegram_en_drive(msg, chat_id)
            if nombre:
                enviar_texto(
                    f"Foto recibida y guardada en 1_entrada de Google Drive: {nombre}.\n"
                    "Quedó registrada para procesar. Enviá la otra foto ahora; cuando termines, el sistema analizará el lote y te enviará el cartel para publicar, modificar o cancelar.",
                    chat_id=chat_id)
                return
        except Exception as e:
            log.exception("No pude guardar en Drive una imagen recibida por Telegram")
            enviar_texto(f"No pude guardar la foto en 1_entrada de Google Drive: {e}. No se inició el análisis; podés volver a enviarla.", chat_id=chat_id)
            return
    texto = (msg.get("text") or msg.get("caption") or "").strip()
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
    if sin_foto:
        tiene_producto = re.search(r"(?im)^\s*producto\s*:\s*.+$", texto)
        tiene_precio = re.search(
            r"(?im)^\s*(precio|contado|efectivo|transferencia|lista)\s*:\s*\$?\s*[\d.,]+\s*$",
            texto,
        )
        if not (tiene_producto and tiene_precio):
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
    precios = [{"etiqueta": p["etiqueta"], "costo": int(p["costo"]),
                "venta": round(int(p["costo"]) * (1 + porcentaje / 100))} for p in detectados[:3]]
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
        _regenerar(rec, chat_id)
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
    state["seen_drive_ids"] = list(seen)[-1500:]
    save_state()
    if not pending:
        log.info("No hay imágenes pendientes en 1_entrada.")
        return
    latest = max(x.get("last_seen", x.get("first_seen", now)) for x in pending.values())
    # Dar tiempo a que termine la carga de todas las fotos de un lote.
    if now - latest < 30:
        log.info("Hay imágenes recién detectadas; se procesarán en el próximo ciclo para dejar terminar la carga.")
        return
    lote_ids = list(pending.keys())
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
    response = requests.get(f"{API}/getUpdates", params=params, timeout=max(15, timeout + 10))
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(payload.get("description", "getUpdates falló"))
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
    """Mantiene activa cada ejecución: long polling de Telegram y revisión frecuente de Drive."""
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("Falta PUBLISHER_TELEGRAM_BOT_TOKEN en los secretos del repositorio.")
    if not TELEGRAM_CHAT_ID or not ID_ENTRADA or not ID_PUBLICAR:
        raise RuntimeError("Faltan TELEGRAM_CHAT_ID o IDs de carpetas de Drive en la configuración.")
    load_state()
    duracion = max(60, min(270, int(os.environ.get("CLOUD_WORK_SECONDS", "270"))))
    intervalo_drive = max(20, min(120, int(os.environ.get("CLOUD_DRIVE_INTERVAL_SECONDS", "45"))))
    fin = time.monotonic() + duracion
    proxima_revision_drive = 0.0
    fallos_telegram = 0
    log.info("Publicador en nube iniciado: sondeo continuo de Telegram durante %s s; Drive cada %s s.", duracion, intervalo_drive)
    while time.monotonic() < fin:
        try:
            escuchar_botones_una_vez(timeout=20)
            fallos_telegram = 0
        except Exception:
            fallos_telegram += 1
            log.exception("Error consultando Telegram (%s/3).", fallos_telegram)
            if fallos_telegram >= 3:
                raise
            time.sleep(3)
        ahora = time.monotonic()
        if ahora >= proxima_revision_drive:
            try:
                vigilar_drive_una_vez()
            except Exception:
                log.exception("Falló una revisión de Drive; se conservaron los elementos pendientes para reintento.")
            proxima_revision_drive = time.monotonic() + intervalo_drive
    save_state()
    log.info("Ventana de sondeo terminada; GitHub Actions volverá a ejecutar el flujo programado.")


def main():
    if not TELEGRAM_BOT_TOKEN or "PEGAR" in TELEGRAM_BOT_TOKEN: raise RuntimeError("Falta TELEGRAM_BOT_TOKEN en la configuración; no compartas el token.")
    load_state(); threading.Thread(target=vigilar_drive, daemon=True).start(); escuchar_botones()

if __name__ == "__main__": main()
