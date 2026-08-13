"""
Lectura de patentes desde una foto tomada por el guardia.

Flujo:
1. El celular manda la foto (multipart/form-data) a /ocr/leer-patente/
2. Se prueban variantes de contraste y escala sobre el recorte enviado
3. Solo como último recurso se intenta detectar una región con Haar
4. Se limpia el texto y se valida contra el formato genérico de patente
5. Si el formato calza -> se devuelve la patente candidata (el frontend luego
   llama a VerificarPatenteView para chequear contra la BD)
6. Si el formato NO calza o el OCR no encuentra nada legible -> se devuelve
   ok=False para que la app del guardia muestre el campo de búsqueda manual

Requiere las dependencias Python declaradas en requirements.txt. Pillow se usa
para validar el formato y las dimensiones de los archivos antes de entregarlos
a OpenCV. Además, tesseract-ocr debe instalarse a nivel de sistema operativo.
"""
import io
import logging
import os
import re
from dataclasses import dataclass

import cv2
import numpy as np
import pytesseract
from django.conf import settings
from PIL import Image, UnidentifiedImageError
from rest_framework.parsers import MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from .models import PATENTE_REGEX
from .permissions import EsAdmin, EsGuardia

logger = logging.getLogger("acceso.ocr")

CASCADE_PATH = os.path.join(
    settings.BASE_DIR,
    "acceso",
    "haarcascades",
    "haarcascade_russian_plate_number.xml",
)
_plate_cascade = (
    cv2.CascadeClassifier(CASCADE_PATH)
    if hasattr(cv2, "CascadeClassifier")
    else None
)
if _plate_cascade is None or _plate_cascade.empty():
    logger.warning(
        "El clasificador Haar no cargó desde %s (¿archivo faltante, "
        "vacío o corrupto?). La detección de patente va a saltar directo "
        "al fallback de imagen completa en cada intento.",
        CASCADE_PATH,
    )
TESSERACT_WHITELIST = "-c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
MIME_PERMITIDOS = {"image/jpeg", "image/png", "image/webp"}
FORMATOS_PERMITIDOS = {"JPEG", "PNG", "WEBP"}


class ImagenInvalida(ValueError):
    """Error de entrada seguro para mostrar al cliente, sin detalles internos."""


class OcrNoDisponible(RuntimeError):
    """El motor OCR del servidor no se encuentra o no puede ejecutarse."""


@dataclass
class ResultadoOcr:
    patente: str | None
    variante: str | None
    textos: dict[str, str]


def leer_y_validar_archivo(archivo) -> bytes:
    """Valida metadatos, cabecera y dimensiones antes de ejecutar OpenCV."""
    maximo = settings.OCR_MAX_UPLOAD_BYTES
    if archivo.size == 0:
        raise ImagenInvalida("El archivo está vacío.")
    if archivo.size > maximo:
        raise ImagenInvalida("La imagen excede el tamaño máximo permitido.")
    if archivo.content_type not in MIME_PERMITIDOS:
        raise ImagenInvalida("El tipo de archivo no está permitido.")

    contenido = archivo.read(maximo + 1)
    if not contenido:
        raise ImagenInvalida("El archivo está vacío.")
    if len(contenido) > maximo:
        raise ImagenInvalida("La imagen excede el tamaño máximo permitido.")
    try:
        with Image.open(io.BytesIO(contenido)) as imagen:
            if imagen.format not in FORMATOS_PERMITIDOS:
                raise ImagenInvalida("El contenido no corresponde a una imagen permitida.")
            ancho, alto = imagen.size
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as error:
        raise ImagenInvalida("La imagen recibida no es válida.") from error

    if (
        ancho <= 0 or alto <= 0
        or ancho > settings.OCR_MAX_IMAGE_WIDTH
        or alto > settings.OCR_MAX_IMAGE_HEIGHT
        or ancho * alto > settings.OCR_MAX_IMAGE_PIXELS
    ):
        raise ImagenInvalida("Las dimensiones de la imagen exceden el límite permitido.")
    return contenido


def decodificar_imagen_gris(imagen_bytes: bytes) -> np.ndarray:
    """Decodifica los bytes de la foto y la convierte a escala de grises."""
    arr = np.frombuffer(imagen_bytes, np.uint8)
    try:
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    except cv2.error as error:
        raise ImagenInvalida("No se pudo decodificar la imagen recibida") from error
    if img is None:
        raise ImagenInvalida("No se pudo decodificar la imagen recibida")
    try:
        return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    except cv2.error as error:
        raise ImagenInvalida("No se pudo procesar la imagen recibida") from error


def preprocesar_gris(imagen_gris: np.ndarray) -> np.ndarray:
    """Aplica el preprocesamiento usado antes del OCR."""
    gris = cv2.bilateralFilter(
        imagen_gris, 11, 17, 17)  # reduce ruido, conserva bordes
    _, binaria = cv2.threshold(
        gris, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return binaria


def preprocesar_imagen(imagen_bytes: bytes) -> np.ndarray:
    """Convierte la foto a un formato que facilita la lectura del OCR."""
    return preprocesar_gris(decodificar_imagen_gris(imagen_bytes))


def detectar_regiones_patente(imagen_gris):
    """Devuelve rectángulos candidatos (x, y, w, h), de mayor a menor área."""
    if _plate_cascade is None or _plate_cascade.empty():
        return []

    candidatos = _plate_cascade.detectMultiScale(
        imagen_gris, scaleFactor=1.1, minNeighbors=5, minSize=(60, 20)
    )
    candidatos_ordenados = sorted(
        candidatos, key=lambda r: r[2] * r[3], reverse=True)
    logger.info(
        "Cascade: %d región(es) candidata(s) detectada(s): %s",
        len(candidatos_ordenados),
        [tuple(int(v) for v in c) for c in candidatos_ordenados],
    )
    return candidatos_ordenados


def leer_patente_desde_imagen(imagen_procesada: np.ndarray, psm: int = 7) -> tuple[str | None, str]:
    """Ejecuta Tesseract y devuelve la patente validada y el texto crudo."""
    try:
        texto = pytesseract.image_to_string(
            imagen_procesada, config=f"--psm {psm} {TESSERACT_WHITELIST}")
    except (pytesseract.TesseractError, pytesseract.TesseractNotFoundError, OSError) as error:
        logger.exception("Tesseract no está disponible o no pudo ejecutarse")
        raise OcrNoDisponible("El motor OCR no está disponible en el servidor.") from error
    candidata = re.sub(r"[^A-Z0-9]", "", texto.upper())

    # La expresión acepta deliberadamente patentes internacionales y, por ello,
    # puede admitir más falsos positivos alfanuméricos. El guardia siempre debe
    # confirmar o corregir el resultado antes de verificar el vehículo.
    if PATENTE_REGEX.fullmatch(candidata):
        logger.info("Tesseract encontró una patente con formato válido")
        return candidata, texto

    logger.info("Tesseract no encontró una patente con formato válido")
    return None, texto


def _variantes(imagen_gris: np.ndarray):
    """Genera las variantes en el orden recomendado para recortes de patente."""
    yield "gris_psm7", imagen_gris, 7
    escalada = cv2.resize(
        imagen_gris, None, fx=2.5, fy=2.5, interpolation=cv2.INTER_CUBIC
    )
    yield "reescalada_2_5x_psm7", escalada, 7
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(escalada)
    _, otsu = cv2.threshold(clahe, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    yield "clahe_otsu_psm7", otsu, 7
    yield "clahe_otsu_psm6", otsu, 6


def _probar_variantes(imagen_gris: np.ndarray, prefijo: str, textos: dict[str, str]):
    for nombre, imagen, psm in _variantes(imagen_gris):
        clave = f"{prefijo}{nombre}"
        patente, texto = leer_patente_desde_imagen(imagen, psm)
        textos[clave] = texto
        if patente:
            logger.info("Tesseract encontró la patente con la variante %s", clave)
            return patente, clave
    return None, None


def extraer_patente(imagen_bytes: bytes) -> ResultadoOcr:
    """Prueba variantes y devuelve la patente, la ganadora y los textos crudos."""
    logger.info("Procesando una nueva imagen para OCR")
    imagen_gris = decodificar_imagen_gris(imagen_bytes)
    textos = {}

    patente, variante = _probar_variantes(imagen_gris, "", textos)
    if patente:
        return ResultadoOcr(patente, variante, textos)

    # El front envía un recorte ajustado. Haar queda solamente como último recurso.
    candidatos = detectar_regiones_patente(imagen_gris)
    for i, (x, y, w, h) in enumerate(candidatos):
        logger.info("Probando candidato %d/%d: región (x=%d, y=%d, w=%d, h=%d)",
                    i + 1, len(candidatos), x, y, w, h)
        recorte = imagen_gris[y:y + h, x:x + w]
        patente, variante = _probar_variantes(recorte, f"haar_{i + 1}_", textos)
        if patente:
            logger.info("Patente encontrada en candidato %d", i + 1)
            return ResultadoOcr(patente, variante, textos)

    logger.info("Ninguna variante OCR produjo una patente válida")
    return ResultadoOcr(None, None, textos)


class LeerPatenteView(APIView):
    permission_classes = [IsAuthenticated, EsGuardia]
    parser_classes = [MultiPartParser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "ocr_recognition"

    def post(self, request):
        archivo = request.FILES.get("foto")
        if not archivo:
            return Response({"detail": "Falta el archivo 'foto'"}, status=400)

        try:
            resultado = extraer_patente(leer_y_validar_archivo(archivo))
        except OcrNoDisponible as error:
            return Response({"ok": False, "detalle": str(error)}, status=503)
        except ImagenInvalida as error:
            logger.warning("Imagen rechazada por validación o procesamiento")
            return Response({
                "ok": False,
                "detalle": str(error),
            }, status=400)
        except cv2.error:
            logger.warning("OpenCV no pudo ejecutar el procesamiento OCR")
            return Response({
                "ok": False,
                "detalle": "La imagen no se pudo procesar.",
            }, status=400)

        if resultado.patente:
            respuesta = {
                "ok": True,
                "patente": resultado.patente,
                "variante": resultado.variante,
            }
            if settings.DEBUG and request.query_params.get("debug") == "1":
                respuesta["debug"] = {"textos": resultado.textos}
            return Response(respuesta)

        # Fallback: el OCR no logró leer una patente válida
        respuesta = {
            "ok": False,
            "detalle": "No se pudo leer la patente automáticamente. Ingresa manualmente.",
        }
        if settings.DEBUG and request.query_params.get("debug") == "1":
            respuesta["debug"] = {"textos": resultado.textos}
        return Response(respuesta)


class EstadoOcrView(APIView):
    """Diagnóstico administrativo de las dependencias y límites del OCR."""
    permission_classes = [IsAuthenticated, EsAdmin]

    def get(self, request):
        try:
            version_tesseract = str(pytesseract.get_tesseract_version())
            error_tesseract = None
        except (pytesseract.TesseractError, pytesseract.TesseractNotFoundError, OSError) as error:
            logger.exception("No se pudo obtener la versión de Tesseract")
            version_tesseract = None
            error_tesseract = str(error)

        cascade_cargado = bool(
            _plate_cascade is not None and not _plate_cascade.empty()
        )
        return Response({
            "tesseract": {"version": version_tesseract, "error": error_tesseract},
            "opencv": {"version": cv2.__version__},
            "haar": {"cargado": cascade_cargado, "ruta": CASCADE_PATH},
            "limites": {
                "max_upload_bytes": settings.OCR_MAX_UPLOAD_BYTES,
                "max_image_width": settings.OCR_MAX_IMAGE_WIDTH,
                "max_image_height": settings.OCR_MAX_IMAGE_HEIGHT,
                "max_image_pixels": settings.OCR_MAX_IMAGE_PIXELS,
                "throttle_rate": settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]["ocr_recognition"],
            },
        })
