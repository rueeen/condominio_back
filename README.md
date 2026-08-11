# Condominio Back

Backend de una aplicación de control de acceso para condominios, construido con
Django y Django REST Framework. Gestiona usuarios por rol, visitas temporales o
permanentes, códigos QR, vehículos, estacionamientos y lectura de patentes por
OCR.

## Requisitos del sistema

- Python 3.10 o superior.
- `pip` y soporte para entornos virtuales (`venv`).
- **Tesseract OCR (`tesseract-ocr`) instalado en el sistema operativo.** No es
  una dependencia de `pip`: si falta, los intentos de leer una patente no podrán
  ejecutar el motor OCR y la API devolverá un error de procesamiento.

En Debian o Ubuntu, Tesseract se puede instalar con:

```bash
sudo apt update
sudo apt install tesseract-ocr
```

Las dependencias Python incluyen Pillow, que valida el formato y las dimensiones
de las imágenes recibidas, y OpenCV y NumPy, que realizan su procesamiento.

## Configuración local

Desde la raíz del repositorio:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

Antes de iniciar Django, completa al menos `SECRET_KEY` en `.env`. La API queda
disponible por defecto en `http://127.0.0.1:8000/api/`.

## Tests

Con el entorno virtual activo y las dependencias instaladas:

```bash
python manage.py test acceso
```

## Roles

| Rol | Capacidades principales |
| --- | --- |
| **Admin** | Administra propietarios, guardias y estacionamientos; revisa y aprueba o rechaza vehículos; consulta el historial de ingresos. |
| **Propietario** | Registra visitas temporales o permanentes y sus datos de acceso; consulta sus estacionamientos y solicita vehículos asociados. |
| **Guardia** | Verifica el acceso por documento, QR o patente y utiliza el OCR de patentes durante el control de ingreso. |

## Despliegue en PythonAnywhere

Después de **cada** `git pull`, activa el entorno virtual y ejecuta, antes de
presionar **Reload** en la aplicación web:

```bash
pip install -r requirements.txt
python manage.py migrate
```

Este paso aplica nuevas dependencias y cambios de base de datos; omitirlo puede
dejar el código desplegado incompatible con el entorno o el esquema existente.
