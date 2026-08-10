from datetime import timedelta
from unittest.mock import patch

import cv2
import numpy as np

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from acceso import ocr
from acceso.models import Estacionamiento, Vehiculo
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


class GuardiaAdminEndpointTests(TestCase):
    def setUp(self):
        cache.clear()
        user_model = get_user_model()
        self.admin = user_model.objects.create_user(
            username="admin-guardias", password="clave-segura", rol="admin"
        )
        self.propietario = user_model.objects.create_user(
            username="propietario-guardias", password="clave-segura",
            rol="propietario", torre=2, departamento=201,
        )
        self.guardia = user_model.objects.create_user(
            username="guardia-existente", password="clave-segura", rol="guardia"
        )
        self.client = APIClient()

    def test_admin_crea_guardia_sin_exponer_password_y_puede_autenticarse(self):
        self.client.force_authenticate(self.admin)
        response = self.client.post(
            "/api/guardias/",
            {
                "username": "guardia-nuevo",
                "first_name": "Ana",
                "last_name": "Pérez",
                "password": "secreto-seguro",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 201)
        self.assertNotIn("password", response.data)
        usuario = get_user_model().objects.get(username="guardia-nuevo")
        self.assertEqual(usuario.rol, "guardia")
        self.assertTrue(usuario.check_password("secreto-seguro"))

        self.client.force_authenticate(user=None)
        token_response = self.client.post(
            "/api/token/",
            {"username": "guardia-nuevo", "password": "secreto-seguro"},
            format="json",
        )
        self.assertEqual(token_response.status_code, 200)
        token = AccessToken(token_response.data["access"])
        self.assertEqual(token["rol"], "guardia")

    def test_listado_tampoco_expone_password(self):
        self.client.force_authenticate(self.admin)

        response = self.client.get("/api/guardias/")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["results"])
        self.assertTrue(all("password" not in item for item in response.data["results"]))

    def test_propietario_y_guardia_no_pueden_crear_guardias(self):
        payload = {"username": "sin-permiso", "password": "secreto-seguro"}
        for usuario in (self.propietario, self.guardia):
            with self.subTest(rol=usuario.rol):
                self.client.force_authenticate(usuario)
                response = self.client.post("/api/guardias/", payload, format="json")
                self.assertEqual(response.status_code, 403)


class VehiculoEstadoFilterTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.admin = user_model.objects.create_user(
            username="admin-filtro", password="clave-segura", rol="admin"
        )
        self.propietario = user_model.objects.create_user(
            username="propietario-filtro", password="clave-segura",
            rol="propietario", torre=3, departamento=301,
        )
        Vehiculo.objects.bulk_create([
            Vehiculo(patente=f"ABCD{numero:02d}", propietario=self.propietario)
            for numero in range(51)
        ])
        Vehiculo.objects.create(
            patente="WXYZ99", propietario=self.propietario,
            estado=Vehiculo.Estado.APROBADO,
        )
        self.client = APIClient()
        self.client.force_authenticate(self.admin)

    def test_filtro_pagina_solo_sobre_estado_solicitado(self):
        response = self.client.get("/api/vehiculos/?estado=pendiente")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 51)
        self.assertEqual(len(response.data["results"]), 50)
        self.assertIsNotNone(response.data["next"])
        self.assertIsNone(response.data["previous"])
        self.assertTrue(all(
            vehiculo["estado"] == Vehiculo.Estado.PENDIENTE
            for vehiculo in response.data["results"]
        ))

    def test_estado_invalido_se_ignora(self):
        response = self.client.get("/api/vehiculos/?estado=inventado")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 52)


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


class VerificacionDocumentoGuardiaTests(TestCase):
    def setUp(self):
        cache.clear()
        usuarios = get_user_model()
        self.guardia = usuarios.objects.create_user(username="guardia-verifica", rol="guardia")
        self.propietarios = [
            usuarios.objects.create_user(
                username=f"prop-verifica-{i}", rol="propietario", torre=1, departamento=101 + i
            )
            for i in range(3)
        ]
        self.client = APIClient()
        self.client.force_authenticate(self.guardia)
        self.url = "/api/guardia/verificar-rut/"

    def autorizar(self, propietario, tipo="rut", numero="12345678-5", **datos):
        ahora = timezone.now()
        return Visitante.objects.create(
            propietario=propietario,
            tipo_documento=tipo,
            numero_documento=numero,
            pais_documento=datos.get("pais_documento", ""),
            nombre=datos.get("nombre", "Visita autorizada"),
            fecha_inicio=datos.get("fecha_inicio", ahora - timedelta(minutes=5)),
            fecha_fin=datos.get("fecha_fin", ahora + timedelta(hours=1)),
        )

    def verificar(self, tipo="rut", numero="12.345.678-5", **datos):
        return self.client.post(self.url, {
            "tipo_documento": tipo, "numero_documento": numero, **datos
        }, format="json")

    def test_rut_sin_autorizacion(self):
        response = self.verificar()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["permitido"])

    def test_rut_con_una_autorizacion_devuelve_id_exacto(self):
        visita = self.autorizar(self.propietarios[0])
        response = self.verificar()
        self.assertEqual(response.data["id_autorizacion"], visita.pk)
        self.assertTrue(response.data["permitido"])
        self.assertEqual(set(response.data), {"permitido", "id_autorizacion", "nombre", "unidad", "fecha_fin"})

    def test_rut_con_varias_autorizaciones(self):
        visitas = [self.autorizar(propietario) for propietario in self.propietarios[:2]]
        response = self.verificar()
        self.assertTrue(response.data["requiere_seleccion"])
        self.assertEqual(
            {item["id_autorizacion"] for item in response.data["autorizaciones"]},
            {visita.pk for visita in visitas},
        )
        self.assertTrue(all(set(item) == {"id_autorizacion", "nombre", "unidad", "vigencia"}
                            for item in response.data["autorizaciones"]))

    def test_pasaporte_con_autorizacion(self):
        visita = self.autorizar(self.propietarios[0], "pasaporte", "PA 123456", pais_documento="Argentina")
        response = self.verificar("pasaporte", " pa   123456 ", pais_documento=" argentina ")
        self.assertEqual(response.data["id_autorizacion"], visita.pk)

    def test_dni_con_autorizacion(self):
        visita = self.autorizar(self.propietarios[0], "dni", "AR-123.456", pais_documento="Argentina")
        response = self.verificar("dni", " ar-123.456 ", pais_documento="Argentina")
        self.assertEqual(response.data["id_autorizacion"], visita.pk)

    def test_documento_extranjero_con_varias_autorizaciones(self):
        for propietario in self.propietarios[:2]:
            self.autorizar(propietario, "pasaporte", "XY999", pais_documento="Perú")
        response = self.verificar("pasaporte", "xy999", pais_documento="perú")
        self.assertEqual(len(response.data["autorizaciones"]), 2)

    def test_autorizacion_expirada_no_es_vigente(self):
        ahora = timezone.now()
        self.autorizar(self.propietarios[0], fecha_inicio=ahora - timedelta(hours=2),
                       fecha_fin=ahora - timedelta(hours=1))
        self.assertFalse(self.verificar().data["permitido"])

    def test_autorizacion_futura_no_es_vigente(self):
        ahora = timezone.now()
        self.autorizar(self.propietarios[0], fecha_inicio=ahora + timedelta(hours=1),
                       fecha_fin=ahora + timedelta(hours=2))
        self.assertFalse(self.verificar().data["permitido"])


@override_settings(
    OCR_MAX_UPLOAD_BYTES=100_000,
    OCR_MAX_IMAGE_WIDTH=500,
    OCR_MAX_IMAGE_HEIGHT=500,
    OCR_MAX_IMAGE_PIXELS=100_000,
)
class OCRSeguridadEndpointTests(TestCase):
    def setUp(self):
        cache.clear()
        self.guardia = get_user_model().objects.create_user(username="guardia-ocr", rol="guardia")
        self.client = APIClient()
        self.client.force_authenticate(self.guardia)
        self.url = "/api/ocr/leer-patente/"

    @staticmethod
    def imagen(ancho=240, alto=120):
        matriz = np.full((alto, ancho, 3), 255, dtype=np.uint8)
        ok, contenido = cv2.imencode(".jpg", matriz)
        assert ok
        return contenido.tobytes()

    def subir(self, contenido, content_type="image/jpeg", nombre="foto.jpg", url=None):
        archivo = SimpleUploadedFile(nombre, contenido, content_type=content_type)
        return self.client.post(url or self.url, {"foto": archivo}, format="multipart")

    def test_rechaza_archivo_vacio(self):
        self.assertEqual(self.subir(b"").status_code, 400)

    def test_rechaza_texto_declarado_como_imagen(self):
        self.assertEqual(self.subir(b"esto no es una imagen").status_code, 400)

    def test_rechaza_archivo_corrupto(self):
        self.assertEqual(self.subir(b"\xff\xd8\xffcontenido-corrupto").status_code, 400)

    def test_rechaza_imagen_demasiado_grande(self):
        self.assertEqual(self.subir(self.imagen(501, 100)).status_code, 400)

    @patch("acceso.ocr.extraer_patente", return_value="ABCD12")
    def test_acepta_imagen_valida(self, extraer):
        response = self.subir(self.imagen())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {"ok": True, "patente": "ABCD12"})

    @patch("acceso.ocr.extraer_patente", return_value=None)
    def test_ocr_sin_coincidencias_mantiene_fallback_manual(self, extraer):
        response = self.subir(self.imagen())
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["ok"])

    @patch("acceso.ocr.extraer_patente", return_value=None)
    def test_throttling_especifico_de_ocr(self, extraer):
        for _ in range(10):
            self.assertEqual(self.subir(self.imagen()).status_code, 200)
        self.assertEqual(self.subir(self.imagen()).status_code, 429)

    @patch("acceso.ocr.detectar_patente_en_imagen", return_value=None)
    def test_throttling_especifico_de_deteccion(self, detectar):
        url = "/api/ocr/detectar-patente/"
        for _ in range(30):
            self.assertEqual(self.subir(self.imagen(), url=url).status_code, 200)
        self.assertEqual(self.subir(self.imagen(), url=url).status_code, 429)


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
