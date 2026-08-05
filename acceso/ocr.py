"""
Lectura de patentes desde una foto tomada por el guardia.

Flujo:
1. El celular manda la foto (multipart/form-data) a /ocr/leer-patente/
2. Se corre OCR sobre la imagen
3. Se limpia el texto y se valida contra el formato de patente chilena
4. Si el formato calza -> se devuelve la patente candidata (el frontend luego
   llama a VerificarPatenteView para chequear contra la BD)
5. Si el formato NO calza o el OCR no encuentra nada legible -> se devuelve
   ok=False para que la app del guardia muestre el campo de búsqueda manual

Requiere: pip install pytesseract pillow opencv-python-headless
Y tener tesseract-ocr instalado a nivel de sistema operativo.
"""
import re

import cv2
import numpy as np
import pytesseract
from rest_framework.parsers import MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import PATENTE_REGEX
from .permissions import EsGuardia


def preprocesar_imagen(imagen_bytes: bytes) -> np.ndarray:
    """Convierte la foto a un formato que facilita la lectura del OCR."""
    arr = np.frombuffer(imagen_bytes, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)

    gris = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    gris = cv2.bilateralFilter(gris, 11, 17, 17)  # reduce ruido, conserva bordes
    _, binaria = cv2.threshold(gris, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return binaria


def extraer_patente(imagen_bytes: bytes) -> str | None:
    """Devuelve la patente candidata (string) o None si no se detectó nada válido."""
    imagen_procesada = preprocesar_imagen(imagen_bytes)

    texto = pytesseract.image_to_string(
        imagen_procesada,
        config="--psm 7 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    )
    # Limpieza: solo letras/números en mayúscula
    candidata = re.sub(r"[^A-Z0-9]", "", texto.upper())

    if PATENTE_REGEX.match(candidata):
        return candidata
    return None


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
