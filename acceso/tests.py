from datetime import timedelta
from unittest.mock import patch

import cv2
import numpy as np

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from acceso import ocr
from acceso.models import Estacionamiento, Vehiculo


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


class VisitanteVigenciaTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.propietario = user_model.objects.create_user(
            username="propietario-visitas",
            password="clave-segura",
            rol="propietario",
            torre=1,
            departamento=101,
        )
        self.client = APIClient()
        self.client.force_authenticate(self.propietario)
        self.url = "/api/visitantes/"
        self.datos_base = {"rut": "12345678-9", "nombre": "Visita"}

    def _crear(self, **fechas):
        return self.client.post(
            self.url,
            {**self.datos_base, **fechas},
            format="json",
        )

    def test_visita_sin_fecha_fin_dura_cuatro_horas(self):
        inicio = timezone.now() + timedelta(days=1)

        response = self._crear(fecha_inicio=inicio.isoformat())

        self.assertEqual(response.status_code, 201)
        visita = self.propietario.visitantes.get()
        self.assertEqual(visita.fecha_fin, visita.fecha_inicio + timedelta(hours=4))
        self.assertTrue(timezone.is_aware(visita.fecha_inicio))
        self.assertTrue(timezone.is_aware(visita.fecha_fin))

    def test_visita_sin_fecha_inicio_usa_hora_actual(self):
        antes = timezone.now()

        response = self._crear()

        despues = timezone.now()
        self.assertEqual(response.status_code, 201)
        visita = self.propietario.visitantes.get()
        self.assertLessEqual(antes, visita.fecha_inicio)
        self.assertLessEqual(visita.fecha_inicio, despues)
        self.assertEqual(visita.fecha_fin, visita.fecha_inicio + timedelta(hours=4))

    def test_visita_con_fechas_explicitas_validas(self):
        inicio = timezone.now() + timedelta(hours=1)
        fin = inicio + timedelta(hours=2)

        response = self._crear(fecha_inicio=inicio.isoformat(), fecha_fin=fin.isoformat())

        self.assertEqual(response.status_code, 201)
        visita = self.propietario.visitantes.get()
        self.assertEqual(visita.fecha_inicio, inicio)
        self.assertEqual(visita.fecha_fin, fin)

    def test_rechaza_fecha_fin_anterior_a_fecha_inicio(self):
        inicio = timezone.now() + timedelta(hours=2)
        fin = inicio - timedelta(minutes=1)

        response = self._crear(fecha_inicio=inicio.isoformat(), fecha_fin=fin.isoformat())

        self.assertEqual(response.status_code, 400)
        self.assertIn("estrictamente posterior", str(response.data["fecha_fin"][0]))

    def test_rechaza_fechas_iguales(self):
        instante = timezone.now() + timedelta(hours=2)

        response = self._crear(
            fecha_inicio=instante.isoformat(), fecha_fin=instante.isoformat()
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("estrictamente posterior", str(response.data["fecha_fin"][0]))

    def test_permite_visita_futura(self):
        inicio = timezone.now() + timedelta(days=7)

        response = self._crear(fecha_inicio=inicio.isoformat())

        self.assertEqual(response.status_code, 201)
        self.assertFalse(response.data["vigente"])

    def test_actualizacion_valida(self):
        response = self._crear()
        visita = self.propietario.visitantes.get()
        nuevo_fin = visita.fecha_fin + timedelta(hours=1)

        response = self.client.patch(
            f"{self.url}{visita.pk}/", {"fecha_fin": nuevo_fin.isoformat()}, format="json"
        )

        self.assertEqual(response.status_code, 200)
        visita.refresh_from_db()
        self.assertEqual(visita.fecha_fin, nuevo_fin)

    def test_actualizacion_invalida_no_modifica_la_visita(self):
        self._crear()
        visita = self.propietario.visitantes.get()
        fin_original = visita.fecha_fin
        inicio_invalido = fin_original + timedelta(minutes=1)

        response = self.client.patch(
            f"{self.url}{visita.pk}/",
            {"fecha_inicio": inicio_invalido.isoformat()},
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("estrictamente posterior", str(response.data["fecha_fin"][0]))
        visita.refresh_from_db()
        self.assertEqual(visita.fecha_fin, fin_original)

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


class VehiculoEstacionamientoInvariantTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.admin = user_model.objects.create_user(
            username="admin-invariante", password="clave", rol="admin"
        )
        self.propietario = user_model.objects.create_user(
            username="propietario-invariante", password="clave", rol="propietario",
            torre=20, departamento=201,
        )
        self.otro = user_model.objects.create_user(
            username="otro-propietario", password="clave", rol="propietario",
            torre=20, departamento=202,
        )
        self.client = APIClient()

    def solicitar(self, patente):
        self.client.force_authenticate(self.propietario)
        return self.client.post("/api/vehiculos/", {"patente": patente}, format="json")

    def resolver(self, vehiculo, **datos):
        self.client.force_authenticate(self.admin)
        return self.client.post(
            f"/api/vehiculos/{vehiculo.pk}/resolver/", datos, format="json"
        )

    def test_propietario_sin_estacionamiento_no_puede_solicitar(self):
        self.assertEqual(self.solicitar("ABCD10").status_code, 400)

    def test_un_estacionamiento_admite_limite_exacto_y_no_mas(self):
        Estacionamiento.objects.create(numero="A1", propietario=self.propietario)

        self.assertEqual(self.solicitar("ABCD11").status_code, 201)
        self.assertEqual(self.solicitar("ABCD12").status_code, 201)
        self.assertEqual(self.solicitar("ABCD13").status_code, 400)

    def test_varios_estacionamientos_multiplican_el_limite(self):
        Estacionamiento.objects.create(numero="A1", propietario=self.propietario)
        Estacionamiento.objects.create(numero="A2", propietario=self.propietario)

        for patente in ("BCDF11", "BCDF12", "BCDF13", "BCDF14"):
            self.assertEqual(self.solicitar(patente).status_code, 201)
        self.assertEqual(self.solicitar("BCDF15").status_code, 400)

    def test_no_elimina_estacionamiento_si_provoca_exceso(self):
        primero = Estacionamiento.objects.create(numero="A1", propietario=self.propietario)
        Estacionamiento.objects.create(numero="A2", propietario=self.propietario)
        for patente in ("CDFG11", "CDFG12", "CDFG13"):
            Vehiculo.objects.create(patente=patente, propietario=self.propietario)
        self.client.force_authenticate(self.admin)

        response = self.client.delete(f"/api/estacionamientos/{primero.pk}/")

        self.assertEqual(response.status_code, 400)
        self.assertTrue(Estacionamiento.objects.filter(pk=primero.pk).exists())

    def test_reasignacion_invalida_conserva_propietario_anterior(self):
        primero = Estacionamiento.objects.create(numero="A1", propietario=self.propietario)
        Estacionamiento.objects.create(numero="A2", propietario=self.propietario)
        for patente in ("DFGH11", "DFGH12", "DFGH13"):
            Vehiculo.objects.create(patente=patente, propietario=self.propietario)
        self.client.force_authenticate(self.admin)

        response = self.client.patch(
            f"/api/estacionamientos/{primero.pk}/",
            {"propietario": self.otro.pk}, format="json",
        )

        self.assertEqual(response.status_code, 400)
        primero.refresh_from_db()
        self.assertEqual(primero.propietario, self.propietario)

    def test_no_resuelve_dos_veces_una_aprobacion(self):
        Estacionamiento.objects.create(numero="A1", propietario=self.propietario)
        vehiculo = Vehiculo.objects.create(patente="EFGH11", propietario=self.propietario)

        self.assertEqual(self.resolver(vehiculo, aprobar=True).status_code, 200)
        self.assertEqual(self.resolver(vehiculo, aprobar=True).status_code, 400)

    def test_no_resuelve_dos_veces_un_rechazo(self):
        Estacionamiento.objects.create(numero="A1", propietario=self.propietario)
        vehiculo = Vehiculo.objects.create(patente="FGHJ11", propietario=self.propietario)

        self.assertEqual(
            self.resolver(vehiculo, aprobar=False, motivo_rechazo="Documento inválido").status_code,
            200,
        )
        self.assertEqual(
            self.resolver(vehiculo, aprobar=False, motivo_rechazo="Otro").status_code,
            400,
        )

    def test_rechazo_exige_motivo(self):
        Estacionamiento.objects.create(numero="A1", propietario=self.propietario)
        vehiculo = Vehiculo.objects.create(patente="GHJK11", propietario=self.propietario)

        response = self.resolver(vehiculo, aprobar=False)

        self.assertEqual(response.status_code, 400)
        vehiculo.refresh_from_db()
        self.assertEqual(vehiculo.estado, Vehiculo.Estado.PENDIENTE)

    def test_aprobacion_revalida_limite_y_limpia_motivo(self):
        vehiculo = Vehiculo.objects.create(
            patente="HJKL11", propietario=self.propietario, motivo_rechazo="anterior"
        )
        self.assertEqual(self.resolver(vehiculo, aprobar=True).status_code, 400)
        Estacionamiento.objects.create(numero="A1", propietario=self.propietario)

        self.assertEqual(self.resolver(vehiculo, aprobar=True).status_code, 200)
        vehiculo.refresh_from_db()
        self.assertEqual(vehiculo.motivo_rechazo, "")
