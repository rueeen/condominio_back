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
import time
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
from .ocr_ia import conteo_diario, extraer_patente_ia
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
    if settings.OCR_USAR_HAAR and hasattr(cv2, "CascadeClassifier")
    else None
)
if settings.OCR_USAR_HAAR and (_plate_cascade is None or _plate_cascade.empty()):
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
    confianza: float | None = None


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


def normalizar_dimensiones(imagen_gris: np.ndarray) -> np.ndarray:
    """Acota imágenes grandes o amplía recortes demasiado bajos."""
    alto, ancho = imagen_gris.shape[:2]
    normalizada = imagen_gris
    if max(alto, ancho) > settings.OCR_MAX_DIM:
        escala = settings.OCR_MAX_DIM / max(alto, ancho)
        normalizada = cv2.resize(
            imagen_gris,
            (round(ancho * escala), round(alto * escala)),
            interpolation=cv2.INTER_AREA,
        )
    elif alto < settings.OCR_MIN_ALTO:
        escala = settings.OCR_MIN_ALTO / alto
        normalizada = cv2.resize(
            imagen_gris,
            (round(ancho * escala), settings.OCR_MIN_ALTO),
            interpolation=cv2.INTER_CUBIC,
        )
    logger.info(
        "Dimensiones OCR originales=%dx%d, normalizadas=%dx%d",
        ancho, alto, normalizada.shape[1], normalizada.shape[0],
    )
    return normalizada


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


def leer_patente_desde_imagen(imagen_procesada: np.ndarray, psm: int = 7):
    """Ejecuta Tesseract y devuelve patente, texto y confianza media."""
    try:
        datos = pytesseract.image_to_data(
            imagen_procesada, config=f"--psm {psm} {TESSERACT_WHITELIST}",
            output_type=pytesseract.Output.DICT,
            timeout=settings.OCR_TIMEOUT_VARIANTE,
        )
    except pytesseract.TesseractNotFoundError as error:
        logger.exception("Tesseract no está disponible o no pudo ejecutarse")
        raise OcrNoDisponible("El motor OCR no está disponible en el servidor.") from error
    except pytesseract.TesseractError as error:
        logger.exception("Tesseract está instalado pero falló al ejecutar el OCR")
        raise OcrNoDisponible("El motor OCR no está disponible en el servidor.") from error
    except RuntimeError as error:
        logger.warning("La variante de Tesseract agotó su timeout: %s", error)
        return None, "", None
    except OSError as error:
        logger.exception("Tesseract no está disponible o no pudo ejecutarse")
        raise OcrNoDisponible("El motor OCR no está disponible en el servidor.") from error

    partes, confianzas = [], []
    for texto, confianza in zip(datos.get("text", []), datos.get("conf", [])):
        parte = re.sub(r"[^A-Z0-9]", "", str(texto).upper())
        if parte:
            partes.append(parte)
            try:
                valor = float(confianza)
                if valor >= 0:
                    confianzas.append(valor)
            except (TypeError, ValueError):
                pass
    texto_crudo = " ".join(str(item) for item in datos.get("text", [])).strip()
    candidata = "".join(partes)
    confianza_media = sum(confianzas) / len(confianzas) if confianzas else None

    # La expresión acepta deliberadamente patentes internacionales y, por ello,
    # puede admitir más falsos positivos alfanuméricos. El guardia siempre debe
    # confirmar o corregir el resultado antes de verificar el vehículo.
    if (PATENTE_REGEX.fullmatch(candidata) and confianza_media is not None
            and confianza_media >= settings.OCR_CONFIANZA_MINIMA):
        logger.info("Tesseract encontró una patente con formato válido")
        return candidata, texto_crudo, confianza_media

    logger.info("Tesseract no encontró una patente con formato válido")
    return None, texto_crudo, confianza_media


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
    yield "enderezada_psm7", _enderezar(imagen_gris), 7


def _enderezar(imagen_gris: np.ndarray, angulo_maximo: float = 20) -> np.ndarray:
    """Corrige inclinaciones moderadas usando los píxeles oscuros como texto."""
    _, invertida = cv2.threshold(
        imagen_gris, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU
    )
    puntos = cv2.findNonZero(invertida)
    if puntos is None or len(puntos) < 3:
        return imagen_gris

    angulo = float(cv2.minAreaRect(puntos)[-1])
    if angulo < -45:
        angulo += 90
    elif angulo > 45:
        angulo -= 90
    if abs(angulo) > angulo_maximo:
        logger.info("Enderezado descartado por ángulo de %.1f grados", angulo)
        return imagen_gris

    alto, ancho = imagen_gris.shape[:2]
    matriz = cv2.getRotationMatrix2D((ancho / 2, alto / 2), angulo, 1.0)
    return cv2.warpAffine(
        imagen_gris,
        matriz,
        (ancho, alto),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_REPLICATE,
    )


def _probar_variantes(imagen_gris, prefijo, textos, inicio):
    mejor = (None, None, None)
    for nombre, imagen, psm in _variantes(normalizar_dimensiones(imagen_gris)):
        if time.monotonic() - inicio >= settings.OCR_PRESUPUESTO_TOTAL:
            return mejor, True
        clave = f"{prefijo}{nombre}"
        patente, texto, confianza = leer_patente_desde_imagen(imagen, psm)
        textos[clave] = texto
        if patente and (mejor[2] is None or confianza > mejor[2]):
            mejor = (patente, clave, confianza)
            if confianza >= settings.OCR_CONFIANZA_ALTA:
                break
        if time.monotonic() - inicio >= settings.OCR_PRESUPUESTO_TOTAL:
            return mejor, True
    return mejor, False


def extraer_patente(imagen_bytes: bytes) -> ResultadoOcr:
    """Prueba variantes y devuelve la patente, la ganadora y los textos crudos."""
    logger.info("Procesando una nueva imagen para OCR")
    imagen_gris = decodificar_imagen_gris(imagen_bytes)
    textos = {}
    inicio = time.monotonic()

    mejor, agotado = _probar_variantes(imagen_gris, "", textos, inicio)
    if agotado:
        logger.warning("OCR superó el presupuesto global")
        return ResultadoOcr(mejor[0], mejor[1], textos, mejor[2])

    # El front envía un recorte ajustado. Haar queda solamente como último recurso.
    candidatos = detectar_regiones_patente(imagen_gris) if settings.OCR_USAR_HAAR else []
    for i, (x, y, w, h) in enumerate(candidatos):
        logger.info("Probando candidato %d/%d: región (x=%d, y=%d, w=%d, h=%d)",
                    i + 1, len(candidatos), x, y, w, h)
        recorte = imagen_gris[y:y + h, x:x + w]
        candidato, agotado = _probar_variantes(
            recorte, f"haar_{i + 1}_", textos, inicio
        )
        if candidato[2] is not None and (mejor[2] is None or candidato[2] > mejor[2]):
            mejor = candidato
        if agotado:
            logger.warning("OCR superó el presupuesto global durante candidatos Haar")
            return ResultadoOcr(mejor[0], mejor[1], textos, mejor[2])

    if mejor[0]:
        return ResultadoOcr(mejor[0], mejor[1], textos, mejor[2])
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
            imagen_bytes = leer_y_validar_archivo(archivo)
            resultado = extraer_patente(imagen_bytes)
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
                "confianza": resultado.confianza,
                "origen": "tesseract",
            }
            logger.info("Lectura de patente resuelta por tesseract")
            if settings.DEBUG and request.query_params.get("debug") == "1":
                respuesta["debug"] = {"textos": resultado.textos}
            return Response(respuesta)

        if settings.OCR_IA_HABILITADO:
            resultado_ia = extraer_patente_ia(imagen_bytes)
            if resultado_ia:
                logger.info("Lectura de patente resuelta por ia")
                return Response({
                    "ok": True,
                    "patente": resultado_ia.patente,
                    "confianza": resultado_ia.confianza,
                    "origen": "ia",
                })

        # Fallback: ninguno de los reconocedores logró una patente válida.
        logger.info("Lectura de patente no resuelta; se requiere ingreso manual")
        respuesta = {
            "ok": False,
            "detalle": "No se pudo leer la patente automáticamente. Ingresa manualmente.",
            "origen": None,
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
            "ia": {
                "habilitado": settings.OCR_IA_HABILITADO,
                "clave_configurada": bool(settings.ANTHROPIC_API_KEY),
                "modelo": settings.OCR_IA_MODELO,
                "timeout": settings.OCR_IA_TIMEOUT,
                "conteo_hoy": conteo_diario(),
            },
            "limites": {
                "max_upload_bytes": settings.OCR_MAX_UPLOAD_BYTES,
                "max_image_width": settings.OCR_MAX_IMAGE_WIDTH,
                "max_image_height": settings.OCR_MAX_IMAGE_HEIGHT,
                "max_image_pixels": settings.OCR_MAX_IMAGE_PIXELS,
                "max_dim": settings.OCR_MAX_DIM,
                "min_alto": settings.OCR_MIN_ALTO,
                "timeout_variante": settings.OCR_TIMEOUT_VARIANTE,
                "presupuesto_total": settings.OCR_PRESUPUESTO_TOTAL,
                "confianza_minima": settings.OCR_CONFIANZA_MINIMA,
                "confianza_alta": settings.OCR_CONFIANZA_ALTA,
                "usar_haar": settings.OCR_USAR_HAAR,
                "throttle_rate": settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]["ocr_recognition"],
            },
        })
