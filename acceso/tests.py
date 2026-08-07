from datetime import timedelta
from unittest.mock import patch

import cv2
import numpy as np

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from acceso import ocr
from acceso.models import Visitante, normalizar_documento


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
        self.datos_base = {
            "tipo_documento": "rut",
            "numero_documento": "12345678-5",
            "nombre": "Visita",
        }

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


class DocumentoVisitanteTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.propietario = user_model.objects.create_user(
            username="prop-doc", rol="propietario", torre=2, departamento=202
        )
        self.guardia = user_model.objects.create_user(
            username="guardia-doc", rol="guardia"
        )
        self.client = APIClient()

    def crear(self, tipo, numero, **extra):
        self.client.force_authenticate(self.propietario)
        return self.client.post(
            "/api/visitantes/",
            {
                "tipo_documento": tipo,
                "numero_documento": numero,
                "nombre": "Persona visitante",
                **extra,
            },
            format="json",
        )

    def test_rut_valido_se_normaliza(self):
        response = self.crear("rut", "12345678-5")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["numero_documento"], "12345678-5")

    def test_rechaza_rut_con_dv_incorrecto(self):
        response = self.crear("rut", "12345678-9")
        self.assertEqual(response.status_code, 400)
        self.assertIn("verificador", str(response.data["numero_documento"]))

    def test_rut_con_puntos_se_normaliza(self):
        response = self.crear("rut", "12.345.678-5")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["numero_documento"], "12345678-5")

    def test_rut_acepta_k_minuscula(self):
        response = self.crear("rut", "6.000.000-k")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["numero_documento"], "6000000-K")

    def test_pasaporte_normaliza_mayusculas_y_espacios(self):
        response = self.crear("pasaporte", "  pa   123456  ", pais_documento="Canadá")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["numero_documento"], "PA 123456")
        self.assertEqual(response.data["pais_documento"], "Canadá")

    def test_dni_extranjero_no_aplica_reglas_de_rut(self):
        response = self.crear("dni", "xy-88.123/4", pais_documento="Argentina")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["numero_documento"], "XY-88.123/4")

    def test_documento_otro_permite_pais_vacio(self):
        response = self.crear("otro", " credencial-77 ")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["numero_documento"], "CREDENCIAL-77")
        self.assertEqual(response.data["pais_documento"], "")

    def test_rechaza_documento_vacio(self):
        response = self.crear("pasaporte", "   ")
        self.assertEqual(response.status_code, 400)
        self.assertIn("numero_documento", response.data)

    def test_evitar_documento_duplicado_para_mismo_propietario(self):
        self.assertEqual(self.crear("pasaporte", "ab 123").status_code, 201)
        response = self.crear("pasaporte", " AB   123 ")
        self.assertEqual(response.status_code, 400)

    def test_guardia_encuentra_autorizacion_extranjera_normalizada(self):
        self.assertEqual(self.crear("pasaporte", "pa123456").status_code, 201)
        self.client.force_authenticate(self.guardia)
        response = self.client.post(
            "/api/guardia/verificar-rut/",
            {"tipo_documento": "pasaporte", "numero_documento": " pa123456 "},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["permitido"])

    def test_normalizador_no_impone_formato_dni_por_pais(self):
        self.assertEqual(normalizar_documento("dni", "a-1/234.zz"), "A-1/234.ZZ")


class MigracionDocumentoVisitanteTests(TransactionTestCase):
    reset_sequences = True

    def test_migracion_conserva_rut_y_registro_existente(self):
        executor = MigrationExecutor(connection)
        executor.migrate([("acceso", "0005_alter_visitante_fecha_fin")])
        estado_anterior = executor.loader.project_state(
            [("acceso", "0005_alter_visitante_fecha_fin")]
        ).apps
        UsuarioAnterior = estado_anterior.get_model("acceso", "Usuario")
        VisitanteAnterior = estado_anterior.get_model("acceso", "Visitante")
        propietario = UsuarioAnterior.objects.create(
            username="prop-migracion", rol="propietario", torre=3, departamento=301
        )
        visitante = VisitanteAnterior.objects.create(
            rut="12345678-5",
            nombre="Visita histórica",
            propietario=propietario,
            fecha_inicio=timezone.now(),
            fecha_fin=timezone.now() + timedelta(hours=1),
        )

        executor = MigrationExecutor(connection)
        executor.migrate([("acceso", "0006_documentos_visitante")])
        estado_nuevo = executor.loader.project_state(
            [("acceso", "0006_documentos_visitante")]
        ).apps
        VisitanteNuevo = estado_nuevo.get_model("acceso", "Visitante")
        migrado = VisitanteNuevo.objects.get(pk=visitante.pk)
        self.assertEqual(migrado.tipo_documento, "rut")
        self.assertEqual(migrado.numero_documento, "12345678-5")
        self.assertEqual(migrado.nombre, "Visita histórica")

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
