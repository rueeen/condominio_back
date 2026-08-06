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
    gris = cv2.bilateralFilter(imagen_gris, 11, 17, 17)  # reduce ruido, conserva bordes
    _, binaria = cv2.threshold(gris, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
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
    return sorted(candidatos, key=lambda r: r[2] * r[3], reverse=True)


def leer_patente_desde_imagen(imagen_procesada: np.ndarray) -> str | None:
    """Ejecuta Tesseract y devuelve una patente válida si el texto calza."""
    texto = pytesseract.image_to_string(imagen_procesada, config=TESSERACT_CONFIG)
    candidata = re.sub(r"[^A-Z0-9]", "", texto.upper())

    if PATENTE_REGEX.match(candidata):
        return candidata
    return None


def extraer_patente(imagen_bytes: bytes) -> str | None:
    """Devuelve la patente candidata (string) o None si no se detectó nada válido."""
    imagen_gris = decodificar_imagen_gris(imagen_bytes)

    for x, y, w, h in detectar_regiones_patente(imagen_gris):
        recorte = imagen_gris[y:y + h, x:x + w]
        patente = leer_patente_desde_imagen(preprocesar_gris(recorte))
        if patente:
            return patente

    # Red de seguridad: mantiene el flujo anterior sobre la imagen completa.
    return leer_patente_desde_imagen(preprocesar_gris(imagen_gris))


class LeerPatenteView(APIView):
    permission_classes = [IsAuthenticated, EsGuardia]
    parser_classes = [MultiPartParser]

    def post(self, request):
        archivo = request.FILES.get("foto")
        if not archivo:
            return Response({"detail": "Falta el archivo 'foto'"}, status=400)

        patente = extraer_patente(archivo.read())

        if patente:
            return Response({"ok": True, "patente": patente})

        # Fallback: el OCR no logró leer una patente válida
        return Response({
            "ok": False,
            "detalle": "No se pudo leer la patente automáticamente. Ingresa manualmente.",
        })
