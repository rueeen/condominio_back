"""
WSGI config for condominio_back project.

It exposes the WSGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/5.2/howto/deployment/wsgi/
"""

from django.core.wsgi import get_wsgi_application
import os
os.environ["OMP_THREAD_LIMIT"] = "1"


os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'condominio_back.settings')

application = get_wsgi_application()
