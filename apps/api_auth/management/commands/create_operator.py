from __future__ import annotations

import getpass

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError, CommandParser


class Command(BaseCommand):
    help = (
        "Create the single operator account (prompts for the password). "
        "Refuses when any user already exists: Fleet has no signup."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("username")

    def handle(self, *args: object, **options: object) -> None:
        user_model = get_user_model()
        username = str(options["username"])
        if user_model.objects.exists():
            raise CommandError("A user already exists: Fleet has a single operator.")
        password = getpass.getpass("Password: ")
        if password != getpass.getpass("Password (again): "):
            raise CommandError("The two passwords do not match.")
        try:
            validate_password(password, user=user_model(username=username))
        except ValidationError as exc:
            raise CommandError(" ".join(exc.messages)) from exc
        user_model.objects.create_user(
            username=username, password=password, is_staff=False, is_superuser=False
        )
        self.stdout.write(self.style.SUCCESS(f"Operator {username!r} created."))
