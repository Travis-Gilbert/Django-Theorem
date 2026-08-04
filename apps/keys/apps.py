from django.apps import AppConfig


class KeysConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.keys"
    label = "keys"
    verbose_name = "API Keys"
