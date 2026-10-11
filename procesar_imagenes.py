# -*- coding: utf-8 -*-
"""Google Drive, OCR y diseño de carteles de Revendedor WA."""
from __future__ import annotations
import io, os, re
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageEnhance
import pytesseract
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload
from config import ID_ENTRADA, ID_PROCESADAS, ID_PUBLICAR, GANANCIA_PORCENTAJE

BASE_DIR = Path(__file__).resolve().parent
SCOPES = ["https://www.googleapis.com/auth/drive"]
EXTENSIONES_IMAGEN = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
for ruta in (r"C:\Program Files\Tesseract-OCR\tesseract.exe", r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"):
    if os.path.exists(ruta):
        pytesseract.pytesseract.tesseract_cmd = ruta
        break


def obtener_servicio_drive():
    cred_path, token_path = BASE_DIR / "credentials.json", BASE_DIR / "token.json"
    if not cred_path.exists():
        raise FileNotFoundError(f"No encuentro {cred_path}")
    creds = Credentials.from_authorized_user_file(str(token_path), SCOPES) if token_path.exists() else None
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        elif os.environ.get("REVENDOR_CLOUD_MODE") == "1":
            raise RuntimeError(
                "Google token.json no tiene un refresh_token válido. En la PC, renová la autorización de Drive "
                "y actualizá el secreto GOOGLE_TOKEN_JSON del repositorio. No se puede abrir el navegador durante la ejecución en la nube."
            )
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(cred_path), SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json(), encoding="utf-8")
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def listar_archivos_en_carpeta(service, folder_id):
    q = f"'{folder_id}' in parents and trashed = false"
    salida, page = [], None
    while True:
        res = service.files().list(q=q, pageSize=100, pageToken=page,
            fields="nextPageToken,files(id,name,mimeType,createdTime,parents,webViewLink)",
            includeItemsFromAllDrives=True, supportsAllDrives=True).execute()
        salida.extend(res.get("files", []))
        page = res.get("nextPageToken")
        if not page: break
    return salida


def es_imagen(archivo):
    return archivo.get("mimeType", "").startswith("image/") or Path(archivo.get("name", "")).suffix.lower() in EXTENSIONES_IMAGEN


def descargar_imagen_a_memoria(service, file_id):
    req = service.files().get_media(fileId=file_id, supportsAllDrives=True)
    buff = io.BytesIO(); downloader = MediaIoBaseDownload(buff, req); done = False
    while not done: _, done = downloader.next_chunk()
    buff.seek(0); return buff


def subir_imagen_a_drive(service, file_buffer, nombre_archivo, parent_id, mime_type="image/jpeg"):
    file_buffer.seek(0)
    media = MediaIoBaseUpload(file_buffer, mimetype=mime_type, resumable=True)
    return service.files().create(body={"name": nombre_archivo, "parents": [parent_id]}, media_body=media,
        fields="id,name,webViewLink", supportsAllDrives=True).execute()


def mover_archivo_en_drive(service, file_id, carpeta_origen_id, carpeta_destino_id):
    """Compatibilidad con versiones anteriores. No se usa automáticamente."""
    meta = service.files().get(fileId=file_id, fields="parents", supportsAllDrives=True).execute()
    parents = meta.get("parents", [])
    kwargs = {"fileId": file_id, "addParents": carpeta_destino_id, "fields": "id,parents", "supportsAllDrives": True}
    remove = ",".join(p for p in parents if p == carpeta_origen_id)
    if remove: kwargs["removeParents"] = remove
    return service.files().update(**kwargs).execute()


def leer_texto_ocr(buffer_imagen):
    buffer_imagen.seek(0)
    img = ImageOps.exif_transpose(Image.open(buffer_imagen)).convert("RGB")
    if img.width < 1400:
        factor = min(2.2, 1400 / max(1, img.width))
        img = img.resize((int(img.width * factor), int(img.height * factor)))
    gris = ImageEnhance.Contrast(ImageOps.grayscale(img)).enhance(2.0)
    resultados = []
    for psm in (11, 6, 4):
        try:
            t = pytesseract.image_to_string(gris, lang="spa+eng", config=f"--oem 3 --psm {psm}")
            if t and t.strip() and t.strip() not in resultados: resultados.append(t.strip())
        except Exception: pass
    buffer_imagen.seek(0)
    return "\n\n--- Lectura alternativa ---\n\n".join(resultados)[:12000]


def _fuente(size, bold=False):
    rutas = [r"C:\Windows\Fonts\arialbd.ttf" if bold else r"C:\Windows\Fonts\arial.ttf",
             "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
    for ruta in rutas:
        try: return ImageFont.truetype(ruta, size=max(10, int(size)))
        except Exception: pass
    return ImageFont.load_default()


def pesos(n): return "$" + f"{int(round(float(n))):,}".replace(",", ".")


def crear_cartel_tres_precios(buffer_foto, nombre_producto, precios, porcentaje, detalle=""):
    """Crea un cartel que adapta el alto a entre 1 y 3 precios detectados."""
    if buffer_foto is None: raise ValueError("Falta la foto del producto")
    precios = list(precios or [])[:3]
    if not precios: raise ValueError("No hay precios para mostrar")
    buffer_foto.seek(0)
    img = ImageOps.exif_transpose(Image.open(buffer_foto)).convert("RGB")
    img.thumbnail((1200, 1200), Image.Resampling.LANCZOS)
    ancho, alto = max(900, img.width), img.height
    if img.width != ancho:
        alto = int(img.height * ancho / img.width); img = img.resize((ancho, alto), Image.Resampling.LANCZOS)
    alto_fila = 82
    cabecera = 108
    pie = 76
    alto_panel = cabecera + alto_fila * len(precios) + pie
    canvas = Image.new("RGB", (ancho, alto + alto_panel), (13, 24, 40))
    canvas.paste(img, (0, 0)); d = ImageDraw.Draw(canvas); y = alto
    d.rectangle((0, y, ancho, canvas.height), fill=(13, 24, 40))
    titulo = str(nombre_producto or "Producto")[:80]
    d.text((28, y + 12), titulo, font=_fuente(max(27, ancho // 29), True), fill=(255,255,255))
    d.text((30, y + 55), f"PRECIOS DE REVENTA · MARGEN {porcentaje:g}%", font=_fuente(max(17, ancho // 52), True), fill=(104,235,178))
    yy = y + cabecera
    for p in precios:
        d.rounded_rectangle((24, yy, ancho - 24, yy + alto_fila - 8), radius=13, fill=(29,48,70), outline=(67,92,119), width=2)
        etiqueta = str(p.get("etiqueta", "Precio"))[:24].upper()
        d.text((42, yy + 17), etiqueta, font=_fuente(max(17, ancho // 52), True), fill=(225,232,242))
        valor = pesos(p["venta"]); f = _fuente(max(25, ancho // 32), True); bbox = d.textbbox((0,0), valor, font=f)
        d.text((ancho - (bbox[2]-bbox[0]) - 44, yy + 11), valor, font=f, fill=(255,214,80))
        yy += alto_fila
    d.text((28, canvas.height - 52), "CONSULTÁ O RESERVÁ EL TUYO", font=_fuente(max(18, ancho // 47), True), fill=(255,255,255))
    out = io.BytesIO(); canvas.save(out, "JPEG", quality=94, optimize=True); out.seek(0); return out


def etiquetar_precio(linea):
    """Reconoce modalidades incluso cuando el OCR separa las etiquetas en líneas distintas."""
    s = (linea or "").lower()
    if re.search(r"transfer|transf\b|transf\.", s): return "Transferencia"
    if re.search(r"efectivo|contado|cash", s): return "Efectivo"
    if re.search(r"precio\s*de\s*lista|lista|regular|público|publico|tarjeta\s*de\s*cr[eé]dito", s): return "Lista"
    if re.search(r"mayorista|por mayor", s): return "Mayorista"
    if re.search(r"minorista|por menor", s): return "Minorista"
    if re.search(r"costo|proveedor", s): return "Costo"
    return "Sin identificar"


def extraer_precios(texto):
    """Extrae importes y asigna la etiqueta por proximidad, no solo por la misma línea OCR."""
    texto = (texto or "").replace("\u00a0", " ")
    # Admite formatos 145.000, 145,000, 145000 y números con símbolo monetario.
    patron = re.compile(r"(?<!\d)(?:\d{1,3}(?:[.,]\d{3}){1,2}|\d{5,9})(?!\d)")
    salida, vistos = [], set()
    etiquetas = [
        ("Transferencia", re.compile(r"transferencia|transfer|transf\.?", re.I)),
        ("Efectivo", re.compile(r"valor\s+efectivo|efectivo|contado|cash", re.I)),
        ("Lista", re.compile(r"precio\s+de\s+lista|lista|tarjeta\s+de\s+cr[eé]dito|precio\s+regular", re.I)),
        ("Mayorista", re.compile(r"mayorista|por\s+mayor", re.I)),
        ("Minorista", re.compile(r"minorista|por\s+menor", re.I)),
        ("Costo", re.compile(r"costo|proveedor", re.I)),
    ]
    for m in patron.finditer(texto):
        raw = m.group(0)
        valor = int(re.sub(r"\D", "", raw) or "0")
        if not 1000 <= valor <= 100_000_000:
            continue
        # Ignorar medidas/cantidades sin contexto monetario.
        previo = texto[max(0, m.start()-8):m.start()]
        despues = texto[m.end():min(len(texto), m.end()+8)]
        inicio = max(0, m.start()-130)
        fin = min(len(texto), m.end()+100)
        contexto = texto[inicio:fin]
        linea_inicio = texto.rfind("\n", 0, m.start()) + 1
        linea_fin = texto.find("\n", m.end())
        if linea_fin < 0: linea_fin = len(texto)
        linea = texto[linea_inicio:linea_fin].strip()
        # Los importes con separador de miles, $ o contexto comercial son candidatos válidos.
        if not ("$" in previo or re.search(r"precio|costo|efectivo|transfer|lista|contado|valor|ars|mayorista|minorista", contexto, re.I) or "." in raw or "," in raw):
            continue
        etiqueta = "Sin identificar"
        # Elegir la etiqueta más cercana al importe dentro de la ventana de contexto.
        mejor = None
        for nombre, rx in etiquetas:
            for lab in rx.finditer(contexto):
                abs_pos = inicio + lab.start()
                distancia = abs(abs_pos - m.start())
                # Penalizar un poco las menciones que quedan después del importe.
                score = distancia + (12 if abs_pos > m.start() else 0)
                if mejor is None or score < mejor[0]:
                    mejor = (score, nombre)
        if mejor and mejor[0] <= 115:
            etiqueta = mejor[1]
        else:
            etiqueta = etiquetar_precio(linea)
        clave = (valor, etiqueta)
        if clave in vistos:
            continue
        vistos.add(clave)
        salida.append({"costo": valor, "etiqueta": etiqueta, "linea": linea or contexto.strip()})
    # Si distintas lecturas OCR duplican un mismo importe, conservar una sola ocurrencia por valor.
    unicos, importes = [], set()
    for item in salida:
        if item["costo"] in importes:
            continue
        importes.add(item["costo"])
        unicos.append(item)
    return unicos[:20]


def inferir_nombre_producto(texto):
    ignorar = ("precio", "costo", "efectivo", "transfer", "lista", "contado", "www", "http", "oferta", "mayorista", "minorista")
    for linea in (texto or "").splitlines():
        l = re.sub(r"\s+", " ", linea).strip(" -*•\t")
        if len(l) < 4 or any(k in l.lower() for k in ignorar) or re.fullmatch(r"[\d$.,\s:/-]+", l): continue
        l = re.sub(r"(?<!\w)\$?\s*(?:\d{1,3}(?:[.,]\d{3}){1,2}|\d{5,9})(?!\w)", "", l)
        l = re.sub(r"\s+", " ", l).strip(" -:;,. ")
        if len(l) >= 3: return l[:100]
    return "Producto"
