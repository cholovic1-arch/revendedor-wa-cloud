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
SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/drive.appdata",
]
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
    scopes_granted = set(getattr(creds, "scopes", None) or []) if creds else set()
    scopes_missing = not set(SCOPES).issubset(scopes_granted)

    # Un token existente no obtiene permisos nuevos por cambiar SCOPES en el código.
    # Si falta drive.appdata, hay que autorizar de nuevo en la PC.
    if creds and scopes_missing:
        if os.environ.get("REVENDOR_CLOUD_MODE") == "1":
            raise RuntimeError(
                "GOOGLE_TOKEN_JSON no incluye el permiso OAuth drive.appdata. "
                "En la PC, ejecutá autorizar_drive_appdata.py, guardá el nuevo token "
                "en el secreto GOOGLE_TOKEN_JSON y volvé a ejecutar el flujo."
            )
        creds = None  # Fuerza una autorización interactiva con los permisos nuevos.

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
            creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")
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
    precios = list(precios or [])[:3]
    if not precios: raise ValueError("No hay precios para mostrar")
    if buffer_foto is None:
        # Cartel original sin fotografía: fondo gráfico propio, sin inventar una foto de producto.
        img = Image.new("RGB", (1200, 1200), (13, 24, 40))
        draw = ImageDraw.Draw(img)
        for yy in range(1200):
            t = yy / 1199
            color = (int(10 + 10 * t), int(22 + 12 * t), int(40 + 22 * t))
            draw.line((0, yy, 1200, yy), fill=color)
        draw.ellipse((-240, -220, 420, 440), fill=(18, 62, 91))
        draw.ellipse((850, 760, 1450, 1360), fill=(17, 75, 64))
        draw.rounded_rectangle((115, 250, 1085, 780), radius=48, fill=(19, 38, 59), outline=(104, 235, 178), width=5)
        draw.text((600, 370), "OFERTA ESPECIAL", anchor="mm", font=_fuente(72, True), fill=(255, 214, 80))
        draw.text((600, 485), "PRECIO DE CONTADO", anchor="mm", font=_fuente(46, True), fill=(255, 255, 255))
        draw.text((600, 590), "CONSULTÁ DISPONIBILIDAD", anchor="mm", font=_fuente(32, True), fill=(104, 235, 178))
        buffer_foto = io.BytesIO()
        img.save(buffer_foto, "JPEG", quality=92)
        buffer_foto.seek(0)
    buffer_foto.seek(0)
    img = ImageOps.exif_transpose(Image.open(buffer_foto)).convert("RGB")
    img.thumbnail((1200, 1200), Image.Resampling.LANCZOS)
    ancho, alto = max(900, img.width), img.height
    if img.width != ancho:
        alto = int(img.height * ancho / img.width); img = img.resize((ancho, alto), Image.Resampling.LANCZOS)
    # Panel más alto y precio muy destacado para que se lea en publicaciones desde el celular.
    alto_fila = 126
    cabecera = 112
    pie = 70
    alto_panel = cabecera + alto_fila * len(precios) + pie
    canvas = Image.new("RGB", (ancho, alto + alto_panel), (13, 24, 40))
    canvas.paste(img, (0, 0)); d = ImageDraw.Draw(canvas); y = alto
    d.rectangle((0, y, ancho, canvas.height), fill=(13, 24, 40))
    titulo = str(nombre_producto or "Producto")[:80]
    d.text((28, y + 10), titulo, font=_fuente(max(28, ancho // 27), True), fill=(255,255,255))
    d.text((30, y + 58), "PRECIO DE VENTA AL CONTADO", font=_fuente(max(20, ancho // 43), True), fill=(104,235,178))
    yy = y + cabecera
    for p in precios:
        etiqueta = str(p.get("etiqueta", "Precio")).strip().upper()[:24]
        es_contado = etiqueta in ("EFECTIVO", "CONTADO", "CASH", "PRECIO DE CONTADO", "VENTA AL CONTADO")
        fondo = (15, 91, 67) if es_contado else (29,48,70)
        borde = (104, 235, 178) if es_contado else (67,92,119)
        d.rounded_rectangle((24, yy, ancho - 24, yy + alto_fila - 8), radius=18, fill=fondo, outline=borde, width=4 if es_contado else 2)
        etiqueta_mostrar = ("VENTA AL CONTADO" if es_contado else etiqueta or "PRECIO")[:24]
        d.text((44, yy + 12), etiqueta_mostrar, font=_fuente(max(19, ancho // 43), True), fill=(255,255,255))
        valor = pesos(p["venta"])
        f = _fuente(max(43, min(66, ancho // 17)), True)
        bbox = d.textbbox((0,0), valor, font=f)
        d.text((ancho - (bbox[2]-bbox[0]) - 42, yy + 43), valor, font=f, fill=(255,214,80))
        yy += alto_fila
    d.text((28, canvas.height - 48), "CONSULTÁ STOCK Y PRECIO DE ENVÍO", font=_fuente(max(17, ancho // 48), True), fill=(255,255,255))
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
    """Extrae precios y asocia cada importe con la etiqueta más cercana."""
    texto = (texto or "").replace("\u00a0", " ")
    # Acepta separadores argentinos y números sin separador cuando tienen 5-9 cifras.
    patron = re.compile(r"(?<!\d)\d{1,3}(?:[.,:'’´\s][0-9Oo]{3}){1,2}(?!\d)|(?<!\d)\d[0-9Oo]{4,8}(?!\d)")
    etiquetas = [
        ("Efectivo", re.compile(r"precio\s+de\s+contado|precio\s+contado|valor\s+efectivo|efectivo|contado|cont\.|efvo\.?|cash|1\s+pago", re.I)),
        ("Transferencia", re.compile(r"transferencia|transfer|transf\.?|transfere[nm]cia", re.I)),
        ("Lista", re.compile(r"precio\s+de\s+lista|lista|tarjeta\s*de\s+cr[eé]dito|precio\s+regular", re.I)),
        ("Cuotas", re.compile(r"cuotas?|financiado|financiaci[oó]n|plan\s+de\s+pago|inter[eé]s|tarjeta\s+de\s+cr[eé]dito", re.I)),
        ("Mayorista", re.compile(r"mayorista|por\s+mayor", re.I)),
        ("Minorista", re.compile(r"minorista|por\s+menor", re.I)),
        ("Costo", re.compile(r"costo|proveedor", re.I)),
    ]
    salida, vistos = [], set()
    lineas = texto.splitlines()
    offsets, total = [], 0
    for linea in lineas:
        offsets.append((total, total + len(linea), linea))
        total += len(linea) + 1
    for m in patron.finditer(texto):
        raw = m.group(0).strip()
        valor = int(re.sub(r"\D", "", raw.replace("O", "0").replace("o", "0")) or "0")
        if not 1000 <= valor <= 100_000_000:
            continue
        previo = texto[max(0, m.start()-10):m.start()]
        despues = texto[m.end():min(len(texto), m.end()+10)]
        cerca = texto[max(0, m.start()-80):min(len(texto), m.end()+80)]
        # No descartar un importe solo porque OCR no leyó el signo $ o la etiqueta.
        # Se validará la ambigüedad más adelante antes de generar el cartel.
        if not ("$" in previo + despues or re.search(
            r"precio|costo|efectivo|transfer|lista|contado|valor|ars|mayorista|minorista|combo|cuotas|financ",
            cerca, re.I
        ) or re.search(r"[.,:'’´\s]", raw) or raw.isdigit()):
            continue
        linea_actual = ""
        inicio_linea = 0
        for ini, fin, linea in offsets:
            if ini <= m.start() <= fin:
                linea_actual, inicio_linea = linea, ini
                break
        # La etiqueta de la misma línea más cercana al importe gana.
        candidatos = []
        pos_rel = m.start() - inicio_linea
        for nombre, rx in etiquetas:
            for lab in rx.finditer(linea_actual):
                distancia = min(abs(lab.start() - pos_rel), abs(lab.end() - pos_rel))
                candidatos.append((distancia, nombre, lab.start(), lab.end()))
        etiqueta = "Sin identificar"
        if candidatos:
            distancia, nombre, _, _ = min(candidatos, key=lambda x: x[0])
            # Evita asignar una palabra lejana al precio de otra columna.
            if distancia <= 55:
                etiqueta = nombre
        # Si la etiqueta está en una línea inmediatamente vecina y no hay etiqueta local,
        # asociarla solo cuando esa línea contiene un único importe.
        if etiqueta == "Sin identificar":
            idx_linea = next((i for i, (ini, fin, _) in enumerate(offsets) if ini <= m.start() <= fin), -1)
            vecinos = []
            if idx_linea > 0:
                vecinos.append(offsets[idx_linea-1][2])
            if 0 <= idx_linea < len(offsets)-1:
                vecinos.append(offsets[idx_linea+1][2])
            for vecina in vecinos:
                if len(list(patron.finditer(vecina))) != 1:
                    continue
                for nombre, rx in etiquetas:
                    if rx.search(vecina):
                        etiqueta = nombre
                        break
                if etiqueta != "Sin identificar":
                    break
        li = texto.rfind("\n", 0, m.start()) + 1
        lf = texto.find("\n", m.end())
        if lf < 0:
            lf = len(texto)
        linea = texto[li:lf].strip()
        if valor not in vistos:
            salida.append({"costo": valor, "etiqueta": etiqueta, "linea": linea or raw})
            vistos.add(valor)
    return salida[:30]

def inferir_nombre_producto(texto):
    """Prioriza el nombre comercial y reconoce combos antes que códigos aislados."""
    lineas = [re.sub(r"\s+", " ", x).strip(" -*•\t|=<>") for x in (texto or "").splitlines()]
    ignorar = ("precio", "costo", "efectivo", "transfer", "lista", "contado", "www", "http",
               "oferta", "mayorista", "minorista", "creciendo día a día", "ideal para familias",
               "fácil acceso", "mejor organización", "optimiza el espacio", "rendimiento constante",
               "diseño robusto", "se integra", "mantiene los alimentos", "gran capacidad")
    for i, linea in enumerate(lineas):
        if re.search(r"\bcombo\b", linea, re.I):
            limpia = re.sub(r"(?<!\w)\$?\s*\d{1,3}(?:[.,:'’´\s]\d{3}){1,2}(?!\w)", "", linea)
            limpia = re.sub(r"\s+", " ", limpia).strip(" -:;,. ")
            if len(limpia) >= 7 and not re.fullmatch(r"combo", limpia, re.I):
                return limpia[:100]
            partes = ["Combo"]
            for candidata in lineas[i+1:i+4]:
                low = candidata.lower()
                if not candidata or any(k in low for k in ignorar) or re.search(r"\$?\s*\d[\d.,:'’´ ]{3,}", candidata):
                    continue
                if re.fullmatch(r"[\d$.,\s:/-]+", candidata):
                    continue
                partes.append(candidata)
                if len(" ".join(partes)) >= 65:
                    break
            return " ".join(partes)[:100] if len(partes) > 1 else "Combo de artículos"
    for linea in lineas:
        if re.search(r"\b[A-Z]{1,5}[- ]?\d{2,6}[A-Z]?\b", linea, re.I) and not re.search(r"\$", linea):
            modelo = re.search(r"\b[A-Z]{1,5}[- ]?\d{2,6}[A-Z]?\b", linea, re.I).group(0).replace(" ", "")
            categoria = next((x for x in lineas if re.search(r"freezer|heladera|lavarropa|cocina|microondas|televisor|parrilla|taladro|amoladora", x, re.I)), "")
            return (categoria.title() + " " + modelo).strip()[:100] if categoria else modelo
    for linea in lineas:
        low = linea.lower()
        if len(linea) < 4 or any(k in low for k in ignorar) or re.fullmatch(r"[\d$.,\s:/-]+", linea):
            continue
        if re.search(r"\b\d+\s*(litros|lts|ltrs)\b", low):
            continue
        limpia = re.sub(r"(?<!\w)\$?\s*\d{1,3}(?:[.,:'’´\s]\d{3}){1,2}(?!\w)", "", linea)
        limpia = re.sub(r"\s+", " ", limpia).strip(" -:;,. ")
        if len(limpia) >= 3:
            return limpia[:100]
    return "Producto"

