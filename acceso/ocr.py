"""
Lectura de patentes desde una foto tomada por el guardia.

Flujo:
1. El celular manda la foto (multipart/form-data) a /ocr/leer-patente/
2. Se intenta detectar y recortar la región de la patente
3. Se corre OCR sobre el recorte; si falla, sobre la imagen completa
4. Se limpia el texto y se valida contra el formato de patente chilena
5. Si el formato calza -> se devuelve la patente candidata (el frontend luego
   llama a VerificarPatenteView para chequear contra la BD)
6. Si el formato NO calza o el OCR no encuentra nada legible -> se devuelve
   ok=False para que la app del guardia muestre el campo de búsqueda manual

Requiere: pip install pytesseract pillow opencv-python-headless
Y tener tesseract-ocr instalado a nivel de sistema operativo.
"""
import logging
import os
import re

import cv2
import numpy as np
import pytesseract
from django.conf import settings
from rest_framework.parsers import MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import PATENTE_REGEX
from .permissions import EsGuardia

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
TESSERACT_CONFIG = "--psm 7 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


def decodificar_imagen_gris(imagen_bytes: bytes) -> np.ndarray:
    """Decodifica los bytes de la foto y la convierte a escala de grises."""
    arr = np.frombuffer(imagen_bytes, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("No se pudo decodificar la imagen recibida")
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)


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


def leer_patente_desde_imagen(imagen_procesada: np.ndarray) -> str | None:
    """Ejecuta Tesseract y devuelve una patente válida si el texto calza."""
    texto = pytesseract.image_to_string(
        imagen_procesada, config=TESSERACT_CONFIG)
    candidata = re.sub(r"[^A-Z0-9]", "", texto.upper())

    if PATENTE_REGEX.match(candidata):
        logger.info("Tesseract leyó %r -> limpio %r -> VÁLIDA",
                    texto.strip(), candidata)
        return candidata

    logger.info(
        "Tesseract leyó %r -> limpio %r -> no calza con el formato de patente",
        texto.strip(), candidata,
    )
    return None


def detectar_patente_en_imagen(imagen_bytes: bytes) -> dict | None:
    """
    Detección liviana (SOLO el cascade, sin Tesseract) para que el frontend
    pueda hacer polling frecuente mientras la cámara está activa, sin pagar
    el costo de OCR en cada intento. Devuelve el rectángulo candidato más
    grande y las dimensiones de la imagen recibida, o None si no encontró
    nada.
    """
    imagen_gris = decodificar_imagen_gris(imagen_bytes)
    candidatos = detectar_regiones_patente(imagen_gris)
    if not candidatos:
        return None

    x, y, w, h = candidatos[0]
    alto_img, ancho_img = imagen_gris.shape
    return {
        "x": int(x), "y": int(y), "w": int(w), "h": int(h),
        "imagen_ancho": int(ancho_img), "imagen_alto": int(alto_img),
    }


def extraer_patente(imagen_bytes: bytes) -> str | None:
    """Devuelve la patente candidata (string) o None si no se detectó nada válido."""
    logger.info("Nueva foto recibida: %d bytes", len(imagen_bytes))
    imagen_gris = decodificar_imagen_gris(imagen_bytes)

    candidatos = detectar_regiones_patente(imagen_gris)
    for i, (x, y, w, h) in enumerate(candidatos):
        logger.info("Probando candidato %d/%d: región (x=%d, y=%d, w=%d, h=%d)",
                    i + 1, len(candidatos), x, y, w, h)
        recorte = imagen_gris[y:y + h, x:x + w]
        patente = leer_patente_desde_imagen(preprocesar_gris(recorte))
        if patente:
            logger.info("Patente encontrada en candidato %d: %s",
                        i + 1, patente)
            return patente

    # Red de seguridad: mantiene el flujo anterior sobre la imagen completa.
    logger.info("Ningún candidato del cascade dio una lectura válida (o no hubo "
                "candidatos) -> probando OCR sobre la imagen completa (fallback)")
    patente = leer_patente_desde_imagen(preprocesar_gris(imagen_gris))
    logger.info("Resultado del fallback sobre imagen completa: %s", patente)
    return patente


class DetectarPatenteView(APIView):
    """
    Endpoint liviano para polling desde el frontend mientras la cámara está
    activa: SOLO corre el cascade (rápido), no Tesseract. El frontend lo usa
    para saber cuándo "engancha" una patente y disparar la captura automática,
    sin pagar el costo de OCR en cada frame consultado.
    """
    permission_classes = [IsAuthenticated, EsGuardia]
    parser_classes = [MultiPartParser]

    def post(self, request):
        archivo = request.FILES.get("foto")
        if not archivo:
            return Response({"detail": "Falta el archivo 'foto'"}, status=400)

        try:
            resultado = detectar_patente_en_imagen(archivo.read())
        except ValueError:
            return Response({"detectada": False})

        if resultado:
            return Response({"detectada": True, **resultado})
        return Response({"detectada": False})


class LeerPatenteView(APIView):
    permission_classes = [IsAuthenticated, EsGuardia]
    parser_classes = [MultiPartParser]

    def post(self, request):
        archivo = request.FILES.get("foto")
        if not archivo:
            return Response({"detail": "Falta el archivo 'foto'"}, status=400)

        try:
            patente = extraer_patente(archivo.read())
        except ValueError:
            logger.warning(
                "No se pudo decodificar la imagen recibida", exc_info=True)
            return Response({
                "ok": False,
                "detalle": "La imagen recibida no se pudo procesar. Intenta de nuevo.",
            })

        if patente:
            return Response({"ok": True, "patente": patente})

        # Fallback: el OCR no logró leer una patente válida
        return Response({
            "ok": False,
            "detalle": "No se pudo leer la patente automáticamente. Ingresa manualmente.",
        })
