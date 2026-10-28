from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from care.facility.models import Facility
from hmis.lab_catalogue import replace_lab_catalogue

OUTCOMES = [
    ("deleted", "deleted"),
    ("retired", "retired (a booking uses it, so it can't be deleted)"),
    ("replaced", "rewritten to the starting catalogue"),
    ("added", "added"),
    ("no_tariff", "not added: no tariff row (is billing set up? run setup_billing)"),
]


class Command(BaseCommand):
    help = (
        "Replaces a facility's lab tests (or every facility's with a unit code) with the "
        "starting catalogue in hmis.lab_catalogue.LAB_SEED. Tests not in it are deleted "
        "outright, or retired if a booking uses them; tests in it are rewritten to match, "
        "keeping their tariff price. Dry run by default: it reports what it would do and "
        "changes nothing. Pass --apply to write."
    )

    def add_arguments(self, parser):
        parser.add_argument("--facility", help="Facility id or external id")
        parser.add_argument("--apply", action="store_true", help="Write the changes")

    def handle(self, *args, **options):
        if options["facility"]:
            value = options["facility"]
            lookup = {"id": value} if value.isdigit() else {"external_id": value}
            facilities = list(Facility.objects.filter(**lookup))
            if not facilities:
                msg = f"No facility {value}"
                raise CommandError(msg)
        else:
            facilities = list(
                Facility.objects.filter(unit_code__isnull=False, deleted=False)
            )
            if not facilities:
                raise CommandError("No facility has a unit code yet")

        apply = options["apply"]
        for facility in facilities:
            with transaction.atomic():
                outcome = replace_lab_catalogue(facility)
                if not apply:
                    transaction.set_rollback(True)
            self.stdout.write(f"{facility.name}:")
            for key, label in OUTCOMES:
                if outcome[key]:
                    self.stdout.write(f"  {label}: {', '.join(outcome[key])}")
        if not apply:
            self.stdout.write("Dry run: nothing changed. Pass --apply to write.")
