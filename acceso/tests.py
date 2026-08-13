from datetime import timedelta
from unittest.mock import patch

import cv2
import numpy as np
import pytesseract

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
from acceso.models import Estacionamiento, IngresoLog, Vehiculo
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

    def test_rechaza_email_duplicado_con_mensaje_claro(self):
        get_user_model().objects.create_user(
            username="correo-guardia-existente", rol="guardia",
            email="compartido@example.com",
        )
        self.client.force_authenticate(self.admin)
        response = self.client.post(
            "/api/guardias/",
            {"username": "guardia-correo-duplicado", "password": "secreto-seguro",
             "email": "compartido@example.com"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(str(response.data["email"][0]), "Ya existe una cuenta con ese correo")

    def test_propietario_y_guardia_no_pueden_crear_guardias(self):
        payload = {"username": "sin-permiso", "password": "secreto-seguro"}
        for usuario in (self.propietario, self.guardia):
            with self.subTest(rol=usuario.rol):
                self.client.force_authenticate(usuario)
                response = self.client.post("/api/guardias/", payload, format="json")
                self.assertEqual(response.status_code, 403)


class PropietarioAltaEndpointTests(TestCase):
    def setUp(self):
        cache.clear()
        user_model = get_user_model()
        self.admin = user_model.objects.create_user(
            username="admin-propietarios", password="clave-segura", rol="admin"
        )
        self.propietario = user_model.objects.create_user(
            username="propietario-existente",
            password="clave-segura",
            rol="propietario",
            torre=2,
            departamento=201,
        )
        self.guardia = user_model.objects.create_user(
            username="guardia-propietarios", password="clave-segura", rol="guardia"
        )
        self.client = APIClient()
        self.payload = {
            "username": "propietario-nuevo",
            "first_name": "Ana",
            "last_name": "Pérez",
            "password": "secreto-seguro",
            "torre": 3,
            "departamento": 301,
        }

    def test_admin_crea_propietario_sin_exponer_password_y_puede_autenticarse(self):
        self.client.force_authenticate(self.admin)

        response = self.client.post("/api/propietarios/", self.payload, format="json")

        self.assertEqual(response.status_code, 201)
        self.assertNotIn("password", response.data)
        usuario = get_user_model().objects.get(username="propietario-nuevo")
        self.assertEqual(usuario.rol, "propietario")
        self.assertTrue(usuario.check_password("secreto-seguro"))

        self.client.force_authenticate(user=None)
        token_response = self.client.post(
            "/api/token/",
            {"username": "propietario-nuevo", "password": "secreto-seguro"},
            format="json",
        )
        self.assertEqual(token_response.status_code, 200)

    def test_rechaza_unidad_ocupada_con_mensaje_claro(self):
        self.client.force_authenticate(self.admin)
        payload = {**self.payload, "torre": 2, "departamento": 201}

        response = self.client.post("/api/propietarios/", payload, format="json")

        self.assertEqual(response.status_code, 400)
        self.assertIn("unidad_unica_por_propietario", str(response.data))

    def test_rechaza_email_duplicado_con_mensaje_claro(self):
        self.propietario.email = "compartido@example.com"
        self.propietario.save(update_fields=["email"])
        self.client.force_authenticate(self.admin)
        response = self.client.post(
            "/api/propietarios/",
            {**self.payload, "email": "compartido@example.com"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(str(response.data["email"][0]), "Ya existe una cuenta con ese correo")

    def test_requiere_torre_y_departamento(self):
        self.client.force_authenticate(self.admin)

        for campo in ("torre", "departamento"):
            with self.subTest(campo=campo):
                payload = {**self.payload}
                payload.pop(campo)
                response = self.client.post(
                    "/api/propietarios/", payload, format="json"
                )
                self.assertEqual(response.status_code, 400)
                self.assertIn("torre y departamento", str(response.data))

    def test_propietario_y_guardia_no_pueden_crear_propietarios(self):
        for usuario in (self.propietario, self.guardia):
            with self.subTest(rol=usuario.rol):
                self.client.force_authenticate(usuario)
                response = self.client.post(
                    "/api/propietarios/", self.payload, format="json"
                )
                self.assertEqual(response.status_code, 403)

    def test_get_y_patch_siguen_usando_el_serializer_existente(self):
        self.client.force_authenticate(self.admin)

        detail_response = self.client.get(
            f"/api/propietarios/{self.propietario.pk}/"
        )
        patch_response = self.client.patch(
            f"/api/propietarios/{self.propietario.pk}/",
            {"first_name": "Nombre actualizado"},
            format="json",
        )

        self.assertEqual(detail_response.status_code, 200)
        self.assertIn("estacionamientos", detail_response.data)
        self.assertEqual(patch_response.status_code, 200)
        self.assertEqual(patch_response.data["first_name"], "Nombre actualizado")


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

    def test_visita_sin_fecha_fin_dura_cinco_horas(self):
        inicio = timezone.now() + timedelta(days=1)

        response = self._crear(fecha_inicio=inicio.isoformat())

        self.assertEqual(response.status_code, 201)
        visita = self.propietario.visitantes.get()
        self.assertEqual(visita.fecha_fin, visita.fecha_inicio + timedelta(hours=5))
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
        self.assertEqual(visita.fecha_fin, visita.fecha_inicio + timedelta(hours=5))

    def test_visita_permanente_guarda_fecha_fin_nula(self):
        inicio = timezone.now() - timedelta(days=3)

        response = self._crear(fecha_inicio=inicio.isoformat(), permanente=True)

        self.assertEqual(response.status_code, 201)
        visita = self.propietario.visitantes.get()
        self.assertIsNone(visita.fecha_fin)
        self.assertIsNone(response.data["fecha_fin"])
        self.assertTrue(visita.vigente)

    def test_visita_permanente_ignora_fecha_fin_recibida(self):
        response = self._crear(
            permanente=True,
            fecha_fin=(timezone.now() - timedelta(days=1)).isoformat(),
        )

        self.assertEqual(response.status_code, 201)
        self.assertIsNone(self.propietario.visitantes.get().fecha_fin)

    def test_rechaza_fecha_fin_en_el_pasado(self):
        response = self._crear(
            fecha_inicio=(timezone.now() - timedelta(days=2)).isoformat(),
            fecha_fin=(timezone.now() - timedelta(days=1)).isoformat(),
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("no puede estar en el pasado", str(response.data["fecha_fin"][0]))

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
        self.assertIn(
            "Cancélala si quieres crear una nueva",
            str(response.data["numero_documento"]),
        )

    def test_documento_de_visita_vencida_se_puede_autorizar_nuevamente(self):
        self.assertEqual(self.crear("pasaporte", "ab 123").status_code, 201)
        Visitante.objects.filter(propietario=self.propietario).update(
            fecha_inicio=timezone.now() - timedelta(days=31),
            fecha_fin=timezone.now() - timedelta(days=30),
        )

        response = self.crear("pasaporte", " AB   123 ")

        self.assertEqual(response.status_code, 201)
        self.assertEqual(
            Visitante.objects.filter(propietario=self.propietario).count(), 2
        )

    def test_otro_propietario_puede_autorizar_el_mismo_documento(self):
        self.assertEqual(self.crear("pasaporte", "ab 123").status_code, 201)
        otro_propietario = get_user_model().objects.create_user(
            username="otro-prop-doc",
            rol="propietario",
            torre=3,
            departamento=303,
        )
        self.client.force_authenticate(otro_propietario)

        response = self.client.post(
            "/api/visitantes/",
            {
                "tipo_documento": "pasaporte",
                "numero_documento": " AB   123 ",
                "nombre": "Persona visitante",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(
            Visitante.objects.filter(numero_documento="AB 123").count(), 2
        )

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

    def tearDown(self):
        MigrationExecutor(connection).migrate([("acceso", "0012_usuario_email_unico")])
        super().tearDown()

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


class MigracionTokenQrTests(TransactionTestCase):
    reset_sequences = True

    def tearDown(self):
        MigrationExecutor(connection).migrate([("acceso", "0012_usuario_email_unico")])
        super().tearDown()

    def test_asigna_tokens_unicos_a_visitas_existentes(self):
        executor = MigrationExecutor(connection)
        executor.migrate([("acceso", "0006_documentos_visitante")])
        apps_anteriores = executor.loader.project_state(
            [("acceso", "0006_documentos_visitante")]
        ).apps
        UsuarioAnterior = apps_anteriores.get_model("acceso", "Usuario")
        VisitanteAnterior = apps_anteriores.get_model("acceso", "Visitante")
        propietario = UsuarioAnterior.objects.create(
            username="prop-token-migracion", rol="propietario", torre=4, departamento=401
        )
        ahora = timezone.now()
        for numero in ("12345678-5", "6000000-K"):
            VisitanteAnterior.objects.create(
                tipo_documento="rut", numero_documento=numero, nombre="Histórica",
                propietario=propietario, fecha_inicio=ahora,
                fecha_fin=ahora + timedelta(hours=1),
            )

        executor = MigrationExecutor(connection)
        executor.migrate([("acceso", "0007_visitante_token_qr")])
        apps_nuevas = executor.loader.project_state(
            [("acceso", "0007_visitante_token_qr")]
        ).apps
        tokens = list(
            apps_nuevas.get_model("acceso", "Visitante").objects.values_list(
                "token_qr", flat=True
            )
        )
        self.assertEqual(len(tokens), 2)
        self.assertEqual(len(set(tokens)), 2)
        self.assertNotIn(None, tokens)


class MigracionTokenQrUsuarioTests(TransactionTestCase):
    reset_sequences = True

    def tearDown(self):
        MigrationExecutor(connection).migrate([("acceso", "0012_usuario_email_unico")])
        super().tearDown()

    def test_asigna_tokens_unicos_a_usuarios_existentes(self):
        executor = MigrationExecutor(connection)
        executor.migrate([("acceso", "0010_alter_visitante_numero_documento")])
        apps_anteriores = executor.loader.project_state(
            [("acceso", "0010_alter_visitante_numero_documento")]
        ).apps
        UsuarioAnterior = apps_anteriores.get_model("acceso", "Usuario")
        for numero in range(2):
            UsuarioAnterior.objects.create(
                username=f"usuario-qr-migracion-{numero}", rol="guardia"
            )

        executor = MigrationExecutor(connection)
        executor.migrate([
            ("acceso", "0011_usuario_perfil_qr_ingresolog_residente")
        ])
        apps_nuevas = executor.loader.project_state([
            ("acceso", "0011_usuario_perfil_qr_ingresolog_residente")
        ]).apps
        tokens = list(
            apps_nuevas.get_model("acceso", "Usuario").objects.values_list(
                "token_qr", flat=True
            )
        )
        self.assertEqual(len(tokens), 2)
        self.assertEqual(len(set(tokens)), 2)
        self.assertNotIn(None, tokens)


class MigracionEmailUsuarioTests(TransactionTestCase):
    reset_sequences = True

    def tearDown(self):
        MigrationExecutor(connection).migrate([("acceso", "0012_usuario_email_unico")])
        super().tearDown()

    def test_convierte_emails_vacios_antes_de_aplicar_unicidad(self):
        executor = MigrationExecutor(connection)
        executor.migrate([("acceso", "0011_usuario_perfil_qr_ingresolog_residente")])
        apps_anteriores = executor.loader.project_state([
            ("acceso", "0011_usuario_perfil_qr_ingresolog_residente")
        ]).apps
        UsuarioAnterior = apps_anteriores.get_model("acceso", "Usuario")
        UsuarioAnterior.objects.create(username="sin-email-1", rol="guardia", email="")
        UsuarioAnterior.objects.create(username="sin-email-2", rol="guardia", email="")

        executor = MigrationExecutor(connection)
        executor.migrate([("acceso", "0012_usuario_email_unico")])
        apps_nuevas = executor.loader.project_state([
            ("acceso", "0012_usuario_email_unico")
        ]).apps
        emails = list(
            apps_nuevas.get_model("acceso", "Usuario").objects.order_by("username")
            .values_list("email", flat=True)
        )
        self.assertEqual(emails, [None, None])


class VerificacionQrGuardiaTests(TestCase):
    def setUp(self):
        cache.clear()
        usuarios = get_user_model()
        self.propietario = usuarios.objects.create_user(
            username="prop-qr", rol="propietario", torre=5, departamento=501
        )
        self.otro_propietario = usuarios.objects.create_user(
            username="otro-prop-qr", rol="propietario", torre=5, departamento=502
        )
        self.guardia = usuarios.objects.create_user(username="guardia-qr", rol="guardia")
        ahora = timezone.now()
        self.visita = Visitante.objects.create(
            propietario=self.propietario, tipo_documento="rut",
            numero_documento="12345678-5", nombre="Visita QR",
            fecha_inicio=ahora - timedelta(minutes=5),
            fecha_fin=ahora + timedelta(hours=1),
        )
        self.client = APIClient()
        self.url = "/api/guardia/verificar-qr/"

    def test_propietario_recibe_solo_tokens_de_sus_visitas(self):
        Visitante.objects.create(
            propietario=self.otro_propietario, tipo_documento="rut",
            numero_documento="6000000-K", nombre="Visita ajena",
        )
        self.client.force_authenticate(self.propietario)

        response = self.client.get("/api/visitantes/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["token_qr"], str(self.visita.token_qr))

    def test_token_vigente_permite_y_loguea_documento_no_token(self):
        self.client.force_authenticate(self.guardia)

        response = self.client.post(self.url, {"token": str(self.visita.token_qr)}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {
            "permitido": True,
            "tipo": "visita",
            "id_autorizacion": self.visita.pk,
            "nombre": "Visita QR",
            "unidad": "Torre 5, Depto 501",
            "fecha_fin": self.visita.fecha_fin,
        })
        ingreso = IngresoLog.objects.get()
        self.assertEqual(ingreso.valor_ingresado, "12345678-5")
        self.assertNotIn(str(self.visita.token_qr), ingreso.detalle)

        self.client.force_authenticate(self.propietario)
        historial = self.client.get("/api/ingresos/")
        self.assertEqual(historial.status_code, 403)

    def test_token_de_visita_permanente_antigua_permite_acceso(self):
        Visitante.objects.filter(pk=self.visita.pk).update(
            fecha_inicio=timezone.now() - timedelta(days=3), fecha_fin=None
        )
        self.client.force_authenticate(self.guardia)

        response = self.client.post(
            self.url, {"token": str(self.visita.token_qr)}, format="json"
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["permitido"])
        self.assertIsNone(response.data["fecha_fin"])

    def test_token_expirado_deniega_y_conserva_documento_en_log(self):
        Visitante.objects.filter(pk=self.visita.pk).update(
            fecha_inicio=timezone.now() - timedelta(hours=2),
            fecha_fin=timezone.now() - timedelta(hours=1),
        )
        self.client.force_authenticate(self.guardia)

        response = self.client.post(self.url, {"token": str(self.visita.token_qr)}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["permitido"])
        self.assertEqual(IngresoLog.objects.get().valor_ingresado, "12345678-5")

    def test_token_inexistente_o_malformado_deniega_sin_error(self):
        import uuid

        self.client.force_authenticate(self.guardia)
        for token in (str(uuid.uuid4()), "no-es-un-uuid"):
            with self.subTest(token=token):
                response = self.client.post(self.url, {"token": token}, format="json")
                self.assertEqual(response.status_code, 200)
                self.assertFalse(response.data["permitido"])

    def test_token_residente_permite_y_loguea_unidad_no_token(self):
        self.propietario.first_name = "Residente"
        self.propietario.last_name = "Autorizado"
        self.propietario.save()
        self.client.force_authenticate(self.guardia)

        response = self.client.post(
            self.url, {"token": str(self.propietario.token_qr)}, format="json"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {
            "permitido": True,
            "tipo": "residente",
            "nombre": "Residente Autorizado",
            "unidad": "Torre 5, Depto 501",
        })
        ingreso = IngresoLog.objects.get()
        self.assertEqual(ingreso.tipo, IngresoLog.Tipo.RESIDENTE)
        self.assertEqual(ingreso.valor_ingresado, "Torre 5, Depto 501")
        self.assertNotIn(str(self.propietario.token_qr), ingreso.detalle)

    def test_endpoint_exige_rol_guardia(self):
        self.client.force_authenticate(self.propietario)
        response = self.client.post(self.url, {"token": str(self.visita.token_qr)}, format="json")
        self.assertEqual(response.status_code, 403)


class PerfilEndpointTests(TestCase):
    def setUp(self):
        cache.clear()
        usuarios = get_user_model()
        self.propietario = usuarios.objects.create_user(
            username="prop-perfil", rol="propietario", torre=8, departamento=804
        )
        self.guardia = usuarios.objects.create_user(
            username="guardia-perfil", rol="guardia"
        )
        self.client = APIClient()

    def test_propietario_consulta_y_edita_solo_contacto(self):
        self.client.force_authenticate(self.propietario)
        consulta = self.client.get("/api/perfil/")

        self.assertEqual(consulta.status_code, 200)
        self.assertEqual(consulta.data["token_qr"], str(self.propietario.token_qr))
        self.assertEqual(consulta.data["unidad"], "Torre 8, Depto 804")

        respuesta = self.client.patch(
            "/api/perfil/",
            {
                "email": "residente@example.com",
                "telefono": "+56 9-1234-5678",
                "torre": 2,
                "departamento": 201,
                "rol": "admin",
            },
            format="json",
        )
        self.assertEqual(respuesta.status_code, 200)
        self.propietario.refresh_from_db()
        self.assertEqual(self.propietario.email, "residente@example.com")
        self.assertEqual(self.propietario.telefono, "+56 9-1234-5678")
        self.assertEqual((self.propietario.torre, self.propietario.departamento), (8, 804))
        self.assertEqual(self.propietario.rol, "propietario")

    def test_rechaza_email_invalido_y_oculta_qr_a_guardia(self):
        self.client.force_authenticate(self.propietario)
        respuesta = self.client.patch(
            "/api/perfil/", {"email": "no-es-email"}, format="json"
        )
        self.assertEqual(respuesta.status_code, 400)
        self.assertIn("email", respuesta.data)

        self.client.force_authenticate(self.guardia)
        respuesta = self.client.get("/api/perfil/")
        self.assertEqual(respuesta.status_code, 200)
        self.assertNotIn("token_qr", respuesta.data)

    def test_email_vacio_se_guarda_como_null_y_duplicado_se_rechaza(self):
        otro = get_user_model().objects.create_user(
            username="correo-existente", rol="guardia", email="usado@example.com"
        )
        self.client.force_authenticate(self.propietario)
        duplicado = self.client.patch(
            "/api/perfil/", {"email": otro.email}, format="json"
        )
        self.assertEqual(duplicado.status_code, 400)
        self.assertEqual(
            str(duplicado.data["email"][0]), "Ya existe una cuenta con ese correo"
        )

        vacio = self.client.patch("/api/perfil/", {"email": ""}, format="json")
        self.assertEqual(vacio.status_code, 200)
        self.propietario.refresh_from_db()
        self.assertIsNone(self.propietario.email)

    def test_regenerar_invalida_token_anterior_y_restringe_rol(self):
        token_anterior = self.propietario.token_qr
        self.client.force_authenticate(self.propietario)
        respuesta = self.client.post("/api/perfil/regenerar-qr/", format="json")
        self.assertEqual(respuesta.status_code, 200)
        self.propietario.refresh_from_db()
        self.assertNotEqual(self.propietario.token_qr, token_anterior)
        self.assertEqual(respuesta.data["token_qr"], str(self.propietario.token_qr))

        self.client.force_authenticate(self.guardia)
        token_viejo = self.client.post(
            "/api/guardia/verificar-qr/", {"token": str(token_anterior)}, format="json"
        )
        token_nuevo = self.client.post(
            "/api/guardia/verificar-qr/",
            {"token": str(self.propietario.token_qr)},
            format="json",
        )
        self.assertFalse(token_viejo.data["permitido"])
        self.assertTrue(token_nuevo.data["permitido"])
        self.assertEqual(token_nuevo.data["tipo"], "residente")
        self.assertEqual(
            self.client.post("/api/perfil/regenerar-qr/", format="json").status_code,
            403,
        )

class OCRPatenteTests(TestCase):
    def _imagen_bytes(self):
        imagen = np.full((120, 240, 3), 255, dtype=np.uint8)
        ok, buffer = cv2.imencode(".jpg", imagen)
        self.assertTrue(ok)
        return buffer.tobytes()

    @patch("acceso.ocr.pytesseract.image_to_string")
    @patch("acceso.ocr.detectar_regiones_patente")
    def test_extraer_patente_prueba_variantes_antes_de_haar(self, detectar, tesseract):
        detectar.return_value = [(10, 20, 80, 30)]
        tesseract.side_effect = ["XYZ", "ABCD12"]

        resultado = ocr.extraer_patente(self._imagen_bytes())

        self.assertEqual(resultado.patente, "ABCD12")
        self.assertEqual(resultado.variante, "reescalada_2_5x_psm7")
        self.assertEqual(tesseract.call_count, 2)
        detectar.assert_not_called()

    @patch("acceso.ocr.pytesseract.image_to_string", return_value="ABCD12")
    def test_extraer_patente_usa_gris_como_primera_variante(self, tesseract):
        resultado = ocr.extraer_patente(self._imagen_bytes())

        self.assertEqual(resultado.patente, "ABCD12")
        self.assertEqual(resultado.variante, "gris_psm7")
        tesseract.assert_called_once()

    @patch("acceso.ocr.pytesseract.image_to_string", return_value="XYZ")
    @patch("acceso.ocr.detectar_regiones_patente", return_value=[])
    def test_extraer_patente_devuelve_none_si_no_hay_patente(self, detectar, tesseract):
        resultado = ocr.extraer_patente(self._imagen_bytes())
        self.assertIsNone(resultado.patente)
        self.assertEqual(len(resultado.textos), 4)

    @patch("acceso.ocr.pytesseract.image_to_string")
    def test_tesseract_ausente_es_error_de_servicio(self, tesseract):
        tesseract.side_effect = pytesseract.TesseractNotFoundError()
        with self.assertRaises(ocr.OcrNoDisponible):
            ocr.extraer_patente(self._imagen_bytes())


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
        self.url = "/api/guardia/verificar-documento/"

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

    def test_alias_verificar_rut_mantiene_el_contrato_para_pasaporte(self):
        visita = self.autorizar(
            self.propietarios[0], "pasaporte", "PA 123456", pais_documento="Argentina"
        )

        response = self.client.post(
            "/api/guardia/verificar-rut/",
            {
                "tipo_documento": "pasaporte",
                "numero_documento": " pa   123456 ",
                "pais_documento": " argentina ",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
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
        visita = self.autorizar(self.propietarios[0])
        Visitante.objects.filter(pk=visita.pk).update(
            fecha_inicio=ahora - timedelta(hours=2),
            fecha_fin=ahora - timedelta(hours=1),
        )
        self.assertFalse(self.verificar().data["permitido"])

    def test_autorizacion_futura_no_es_vigente(self):
        ahora = timezone.now()
        self.autorizar(self.propietarios[0], fecha_inicio=ahora + timedelta(hours=1),
                       fecha_fin=ahora + timedelta(hours=2))
        self.assertFalse(self.verificar().data["permitido"])

    def test_autorizacion_permanente_antigua_es_vigente(self):
        visita = self.autorizar(self.propietarios[0])
        Visitante.objects.filter(pk=visita.pk).update(
            fecha_inicio=timezone.now() - timedelta(days=3), fecha_fin=None
        )

        response = self.verificar()

        self.assertTrue(response.data["permitido"])
        self.assertIsNone(response.data["fecha_fin"])


class VerificacionPatenteGuardiaTests(TestCase):
    def setUp(self):
        self.guardia = get_user_model().objects.create_user(
            username="guardia-patente", rol="guardia"
        )
        self.propietario = get_user_model().objects.create_user(
            username="prop-patente", rol="propietario", torre=2, departamento=201
        )
        Vehiculo.objects.create(
            patente="AB1234",
            propietario=self.propietario,
            estado=Vehiculo.Estado.APROBADO,
        )
        self.client = APIClient()
        self.client.force_authenticate(self.guardia)
        self.url = "/api/guardia/verificar-patente/"

    def test_normaliza_separadores_y_mayusculas_antes_de_buscar(self):
        for patente in ("AB-1234", "AB 1234", "ab1234"):
            with self.subTest(patente=patente):
                response = self.client.post(
                    self.url, {"patente": patente}, format="json"
                )
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.data["permitido"])
                self.assertEqual(
                    IngresoLog.objects.latest("pk").valor_ingresado, "AB1234"
                )

    def test_rechaza_patente_invalida_despues_de_normalizar(self):
        response = self.client.post(
            self.url, {"patente": "A-1"}, format="json"
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("patente", response.data)
        self.assertFalse(IngresoLog.objects.exists())


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

    @patch("acceso.ocr.extraer_patente")
    def test_acepta_imagen_valida(self, extraer):
        extraer.return_value = ocr.ResultadoOcr("ABCD12", "gris_psm7", {"gris_psm7": "ABCD12"})
        response = self.subir(self.imagen())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["patente"], "ABCD12")
        self.assertEqual(response.data["variante"], "gris_psm7")

    @patch("acceso.ocr.extraer_patente", return_value=ocr.ResultadoOcr(None, None, {}))
    def test_ocr_sin_coincidencias_mantiene_fallback_manual(self, extraer):
        response = self.subir(self.imagen())
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["ok"])

    @override_settings(DEBUG=True)
    @patch("acceso.ocr.extraer_patente")
    def test_debug_incluye_textos_crudos_solo_si_se_solicita(self, extraer):
        extraer.return_value = ocr.ResultadoOcr(
            None, None, {"gris_psm7": "texto sin formato"}
        )
        response = self.subir(self.imagen(), url=f"{self.url}?debug=1")
        self.assertEqual(
            response.data["debug"]["textos"]["gris_psm7"],
            "texto sin formato",
        )

    @override_settings(DEBUG=False)
    @patch("acceso.ocr.extraer_patente")
    def test_debug_no_expone_textos_en_produccion(self, extraer):
        extraer.return_value = ocr.ResultadoOcr(
            None, None, {"gris_psm7": "texto sin formato"}
        )
        response = self.subir(self.imagen(), url=f"{self.url}?debug=1")
        self.assertNotIn("debug", response.data)

    @patch("acceso.ocr.extraer_patente", return_value=ocr.ResultadoOcr(None, None, {}))
    def test_throttling_especifico_de_ocr(self, extraer):
        for _ in range(60):
            self.assertEqual(self.subir(self.imagen()).status_code, 200)
        self.assertEqual(self.subir(self.imagen()).status_code, 429)

    @patch("acceso.ocr.extraer_patente", side_effect=ocr.OcrNoDisponible(
        "El motor OCR no está disponible en el servidor."
    ))
    def test_tesseract_ausente_devuelve_503(self, extraer):
        response = self.subir(self.imagen())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data, {
            "ok": False,
            "detalle": "El motor OCR no está disponible en el servidor.",
        })


class EstadoOcrEndpointTests(TestCase):
    def setUp(self):
        usuarios = get_user_model()
        self.admin = usuarios.objects.create_user(username="admin-ocr", rol="admin")
        self.guardia = usuarios.objects.create_user(username="guardia-estado-ocr", rol="guardia")
        self.client = APIClient()
        self.url = "/api/ocr/estado/"

    @patch("acceso.ocr.pytesseract.get_tesseract_version", return_value="5.3.0")
    def test_admin_obtiene_diagnostico(self, version):
        self.client.force_authenticate(self.admin)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["tesseract"]["version"], "5.3.0")
        self.assertIn("ruta", response.data["haar"])
        self.assertEqual(response.data["opencv"]["version"], cv2.__version__)
        self.assertIn("throttle_rate", response.data["limites"])

    def test_restringe_diagnostico_a_admin(self):
        self.client.force_authenticate(self.guardia)
        self.assertEqual(self.client.get(self.url).status_code, 403)

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

    def test_admite_patentes_chilenas_y_extranjeras(self):
        Estacionamiento.objects.create(numero="A1", propietario=self.propietario)

        self.assertEqual(self.solicitar("AB1234").status_code, 201)
        self.assertEqual(self.solicitar("1ABC2345").status_code, 201)

    def test_normaliza_separadores_y_evitar_duplicado_activo(self):
        Estacionamiento.objects.create(numero="A1", propietario=self.propietario)

        response = self.solicitar("ab-1234")

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["patente"], "AB1234")
        self.assertEqual(self.solicitar("AB1234").status_code, 400)

    def test_rechaza_patente_fuera_del_rango_luego_de_normalizar(self):
        Estacionamiento.objects.create(numero="A1", propietario=self.propietario)

        response = self.solicitar("A.-1")

        self.assertEqual(response.status_code, 400)
        self.assertIn("entre 4 y 10 caracteres", str(response.data))

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

    def test_eliminar_estacionamiento_conserva_patentes_y_bloquea_nuevas(self):
        primero = Estacionamiento.objects.create(numero="A1", propietario=self.propietario)
        Estacionamiento.objects.create(numero="A2", propietario=self.propietario)
        for patente in ("CDFG11", "CDFG12", "CDFG13"):
            Vehiculo.objects.create(patente=patente, propietario=self.propietario)
        self.client.force_authenticate(self.admin)

        response = self.client.delete(f"/api/estacionamientos/{primero.pk}/")

        self.assertEqual(response.status_code, 204)
        self.assertFalse(Estacionamiento.objects.filter(pk=primero.pk).exists())
        self.assertEqual(Vehiculo.objects.filter(propietario=self.propietario).count(), 3)
        self.assertEqual(self.solicitar("CDFG14").status_code, 400)

    def test_desvincular_reduce_limite_sin_eliminar_patentes(self):
        primero = Estacionamiento.objects.create(numero="A1", propietario=self.propietario)
        Estacionamiento.objects.create(numero="A2", propietario=self.propietario)
        for patente in ("DFGH11", "DFGH12", "DFGH13"):
            Vehiculo.objects.create(patente=patente, propietario=self.propietario)
        self.client.force_authenticate(self.admin)

        response = self.client.patch(
            f"/api/estacionamientos/{primero.pk}/",
            {"propietario": None}, format="json",
        )

        self.assertEqual(response.status_code, 200)
        primero.refresh_from_db()
        self.assertIsNone(primero.propietario)
        self.assertEqual(self.propietario.estacionamientos.count(), 1)
        self.assertEqual(Vehiculo.objects.filter(propietario=self.propietario).count(), 3)
        self.assertEqual(self.solicitar("DFGH14").status_code, 400)

    def test_estacionamiento_libre_se_puede_listar_y_reasignar(self):
        estacionamiento = Estacionamiento.objects.create(numero="LIBRE", propietario=None)
        self.assertEqual(str(estacionamiento), "Estacionamiento LIBRE - sin asignar")
        self.client.force_authenticate(self.admin)

        listado = self.client.get("/api/estacionamientos/")
        response = self.client.patch(
            f"/api/estacionamientos/{estacionamiento.pk}/",
            {"propietario": self.otro.pk}, format="json",
        )

        self.assertEqual(listado.status_code, 200)
        resultados = listado.data.get("results", listado.data)
        self.assertIsNone(resultados[0]["propietario"])
        self.assertEqual(response.status_code, 200)
        estacionamiento.refresh_from_db()
        self.assertEqual(estacionamiento.propietario, self.otro)

    def test_eliminar_propietario_deja_su_estacionamiento_libre(self):
        estacionamiento = Estacionamiento.objects.create(
            numero="A1", propietario=self.propietario
        )

        self.propietario.delete()

        estacionamiento.refresh_from_db()
        self.assertIsNone(estacionamiento.propietario)

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
