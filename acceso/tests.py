from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken


class CondominioTokenObtainPairTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_access_token_includes_frontend_claims(self):
        user_model = get_user_model()
        user_model.objects.create_user(
            username="propietario1",
            password="clave-segura",
            rol="propietario",
            torre=1,
            departamento=101,
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
        self.assertEqual(token["unidad"], "Torre 1, Depto 101")

    def test_token_endpoint_throttles_repeated_anonymous_attempts(self):
        for _ in range(10):
            response = self.client.post(
                "/api/token/",
                {"username": "inexistente", "password": "incorrecta"},
                content_type="application/json",
            )
            self.assertEqual(response.status_code, 401)

        response = self.client.post(
            "/api/token/",
            {"username": "inexistente", "password": "incorrecta"},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 429)


class ListPaginationTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.admin = user_model.objects.create_user(
            username="admin",
            password="clave-segura",
            rol="admin",
        )
        user_model.objects.bulk_create([
            user_model(
                username=f"propietario{numero}",
                rol="propietario",
                torre=(numero % 25) + 1,
                departamento=numero + 1,
            )
            for numero in range(51)
        ])
        self.client = APIClient()
        self.client.force_authenticate(self.admin)

    def test_propietarios_list_uses_page_number_pagination(self):
        response = self.client.get("/api/propietarios/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 51)
        self.assertEqual(len(response.data["results"]), 50)
        self.assertIsNotNone(response.data["next"])
        self.assertIsNone(response.data["previous"])

        second_page = self.client.get("/api/propietarios/?page=2")
        self.assertEqual(second_page.status_code, 200)
        self.assertEqual(len(second_page.data["results"]), 1)


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
