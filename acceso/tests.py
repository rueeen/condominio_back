from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework_simplejwt.tokens import AccessToken


class CondominioTokenObtainPairTests(TestCase):
    def test_access_token_includes_frontend_claims(self):
        user_model = get_user_model()
        user_model.objects.create_user(
            username="propietario1",
            password="clave-segura",
            rol="propietario",
            unidad="A-101",
        )

        response = self.client.post(
            "/api/token/",
            {"username": "propietario1", "password": "clave-segura"},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        token = AccessToken(response.json()["access"])
        self.assertEqual(token["rol"], "propietario")
        self.assertEqual(token["username"], "propietario1")
        self.assertEqual(token["unidad"], "A-101")


from unittest.mock import patch

import cv2
import numpy as np

from acceso import ocr


class OCRPatenteTests(TestCase):
    def _imagen_bytes(self):
        imagen = np.full((120, 240, 3), 255, dtype=np.uint8)
        ok, buffer = cv2.imencode(".jpg", imagen)
        self.assertTrue(ok)
        return buffer.tobytes()

    @patch("acceso.ocr.pytesseract.image_to_string")
    @patch("acceso.ocr.detectar_regiones_patente")
    def test_extraer_patente_lee_primer_recorte_valido(self, detectar, tesseract):
        detectar.return_value = [(10, 20, 80, 30), (0, 0, 60, 20)]
        tesseract.side_effect = ["ruido", "ABCD12"]

        patente = ocr.extraer_patente(self._imagen_bytes())

        self.assertEqual(patente, "ABCD12")
        self.assertEqual(tesseract.call_count, 2)

    @patch("acceso.ocr.pytesseract.image_to_string", return_value="ABCD12")
    @patch("acceso.ocr.detectar_regiones_patente", return_value=[])
    def test_extraer_patente_usa_imagen_completa_como_fallback(self, detectar, tesseract):
        patente = ocr.extraer_patente(self._imagen_bytes())

        self.assertEqual(patente, "ABCD12")
        tesseract.assert_called_once()

    @patch("acceso.ocr.pytesseract.image_to_string", return_value="sin patente")
    @patch("acceso.ocr.detectar_regiones_patente", return_value=[])
    def test_extraer_patente_devuelve_none_si_no_hay_patente(self, detectar, tesseract):
        self.assertIsNone(ocr.extraer_patente(self._imagen_bytes()))
