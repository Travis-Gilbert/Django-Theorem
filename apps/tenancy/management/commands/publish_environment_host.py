"""Publish a theorem-ide-host build for tenants' sprites to install.

    manage.py publish_environment_host <path-to-linux-x86_64-binary>

Uploads the build to the private release bucket under its sha256 and prints
the digest to pin as ``ENVIRONMENT_HOST_SHA256``. Publishing changes no
sprite: pin the digest, then run ``provision_tenant_environment`` for each
tenant (new tenants get it at signup). Publishing the same build again uploads
nothing.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.tenancy.environment import (
    RELEASE_BUCKET_SETTINGS,
    host_object_key,
    missing_settings,
    release_bucket_client,
)

MISSING_OBJECT = {"404", "NoSuchKey", "NotFound"}


def is_linux_x86_64(header: bytes) -> bool:
    """A 64-bit little-endian ELF for x86-64: what every sprite runs."""
    return (
        header[:4] == b"\x7fELF"
        and header[4:6] == b"\x02\x01"
        and int.from_bytes(header[18:20], "little") == 62
    )


class Command(BaseCommand):
    help = "Upload a linux x86_64 theorem-ide-host build and print the digest to pin."

    def add_arguments(self, parser):
        parser.add_argument("binary", type=Path, help="Path to the theorem-ide-host build")

    def handle(self, *args, binary: Path, **options):
        if missing := missing_settings(RELEASE_BUCKET_SETTINGS):
            raise CommandError(f"set {', '.join(missing)} first")
        if not binary.is_file():
            raise CommandError(f"no file at {binary}")
        with binary.open("rb") as build:
            if not is_linux_x86_64(build.read(20)):
                raise CommandError(f"{binary} is not a linux x86_64 executable")
            build.seek(0)
            digest = hashlib.file_digest(build, "sha256").hexdigest()

        bucket = settings.ENVIRONMENT_HOST_BUCKET
        key = host_object_key(digest)
        client = release_bucket_client()
        try:
            client.head_object(Bucket=bucket, Key=key)
            self.stdout.write(f"{key} is already published")
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") not in MISSING_OBJECT:
                raise CommandError(f"reading {bucket}/{key} failed: {error}") from error
            try:
                with binary.open("rb") as body:
                    client.put_object(
                        Bucket=bucket,
                        Key=key,
                        Body=body,
                        ContentType="application/octet-stream",
                    )
            except (BotoCoreError, ClientError) as upload_error:
                raise CommandError(f"uploading {key} failed: {upload_error}") from upload_error
            self.stdout.write(f"published {key}")
        except BotoCoreError as error:
            raise CommandError(f"reading {bucket}/{key} failed: {error}") from error
        self.stdout.write(f"ENVIRONMENT_HOST_SHA256={digest}")
