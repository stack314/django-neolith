"""The URLconf of the host with Django's admin."""

from django.contrib import admin
from django.urls import path

urlpatterns = [path("admin/", admin.site.urls)]
