"""Respaldo de visión para recortes que la tubería local no pudo leer."""
import base64
import json
import logging
import re
from dataclasses import dataclass

import anthropic
import cv2
import numpy as np
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from .models import PATENTE_REGEX

logger = logging.getLogger("acceso.ocr_ia")

SYSTEM_PROMPT = """Devuelve únicamente JSON estricto, sin explicación, markdown ni backticks, con esta forma:
{"patente":"BBBB12","legible":true,"confianza":92,"observacion":""}
Reconoce formatos chilenos habituales: 4 letras y 2 dígitos (actual), 2 letras y 4 dígitos
(antiguo), y placas de moto más cortas. La patente debe contener solamente caracteres
alfanuméricos en mayúscula, sin guiones ni espacios. No adivines: si no se lee con seguridad,
usa legible false, patente null y una confianza de 0 a 100 autoevaluada. Es preferible el ingreso
manual a identificar otro vehículo. Si hay más de una patente, devuelve la más grande y centrada
y anótalo en observacion."""


@dataclass(frozen=True)
class ResultadoOcrIa:
    patente: str
    confianza: float


def clave_conteo_diario():
    return f"ocr_ia:llamadas:{timezone.localdate().isoformat()}"


def conteo_diario():
    return int(cache.get(clave_conteo_diario(), 0) or 0)


def _reservar_llamada():
    clave = clave_conteo_diario()
    if cache.add(clave, 1, timeout=60 * 60 * 48):
        cantidad = 1
    else:
        try:
            cantidad = cache.incr(clave)
        except ValueError:
            cache.set(clave, 1, timeout=60 * 60 * 48)
            cantidad = 1
    if cantidad > settings.OCR_IA_MAX_DIARIO:
        logger.warning("Tope diario de OCR IA alcanzado (%d)", settings.OCR_IA_MAX_DIARIO)
        return False
    return True


def preparar_imagen(imagen_bytes):
    imagen = cv2.imdecode(np.frombuffer(imagen_bytes, np.uint8), cv2.IMREAD_COLOR)
    if imagen is None:
        logger.warning("OCR IA no pudo decodificar la imagen")
        return None
    alto, ancho = imagen.shape[:2]
    if max(alto, ancho) > settings.OCR_IA_MAX_DIM:
        escala = settings.OCR_IA_MAX_DIM / max(alto, ancho)
        imagen = cv2.resize(
            imagen, (round(ancho * escala), round(alto * escala)),
            interpolation=cv2.INTER_AREA,
        )
    ok, codificada = cv2.imencode(".jpg", imagen, [cv2.IMWRITE_JPEG_QUALITY, 80])
    if not ok:
        logger.warning("OCR IA no pudo codificar la imagen como JPEG")
        return None
    contenido = codificada.tobytes()
    logger.info(
        "Imagen enviada a OCR IA: %d bytes, %dx%d",
        len(contenido), imagen.shape[1], imagen.shape[0],
    )
    return contenido


def _texto_respuesta(respuesta):
    partes = [bloque.text for bloque in respuesta.content
              if getattr(bloque, "type", None) == "text"]
    return "".join(partes).strip()


def validar_respuesta(texto):
    try:
        datos = json.loads(texto)
    except (json.JSONDecodeError, TypeError):
        coincidencia = re.search(r"\{.*?\}", texto or "", flags=re.DOTALL)
        if not coincidencia:
            logger.warning("Respuesta malformada de OCR IA: no contiene JSON")
            return None
        try:
            datos = json.loads(coincidencia.group(0))
        except json.JSONDecodeError:
            logger.warning("Respuesta malformada de OCR IA: objeto JSON inválido")
            return None
    if not isinstance(datos, dict) or datos.get("legible") is not True:
        return None
    patente = re.sub(r"[^A-Z0-9]", "", str(datos.get("patente") or "").upper())
    try:
        confianza = float(datos.get("confianza"))
    except (TypeError, ValueError):
        return None
    if (not PATENTE_REGEX.fullmatch(patente)
            or confianza < settings.OCR_IA_CONFIANZA_MINIMA
            or not 0 <= confianza <= 100):
        return None
    return ResultadoOcrIa(patente, confianza)


def extraer_patente_ia(imagen_bytes):
    """Devuelve una lectura validada o None; nunca propaga errores del proveedor."""
    if not settings.OCR_IA_HABILITADO or not settings.ANTHROPIC_API_KEY:
        return None
    if not _reservar_llamada():
        return None
    imagen = preparar_imagen(imagen_bytes)
    if imagen is None:
        return None
    try:
        cliente = anthropic.Anthropic(
            api_key=settings.ANTHROPIC_API_KEY, timeout=settings.OCR_IA_TIMEOUT
        )
        respuesta = cliente.messages.create(
            model=settings.OCR_IA_MODELO,
            max_tokens=128,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {
                    "type": "base64", "media_type": "image/jpeg",
                    "data": base64.b64encode(imagen).decode("ascii"),
                }},
                {"type": "text", "text": "Lee la patente de este recorte."},
            ]}],
        )
        return validar_respuesta(_texto_respuesta(respuesta))
    except anthropic.APITimeoutError:
        logger.warning("Timeout al llamar a OCR IA")
    except anthropic.AuthenticationError:
        logger.error("OCR IA rechazó la autenticación; revisa ANTHROPIC_API_KEY")
    except anthropic.RateLimitError:
        logger.warning("Límite de tasa del proveedor de OCR IA")
    except anthropic.APIConnectionError:
        logger.warning("Error de red al llamar a OCR IA")
    except anthropic.APIError as error:
        logger.warning("Error del proveedor de OCR IA: %s", type(error).__name__)
    except (AttributeError, TypeError, ValueError):
        logger.warning("Respuesta malformada del proveedor de OCR IA")
    return None
