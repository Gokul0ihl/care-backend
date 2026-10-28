from django.core.management.base import BaseCommand, CommandError

from care.facility.models import Facility
from hmis.lab_catalogue import has_lab_tests, seed_lab_catalogue


class Command(BaseCommand):
    help = (
        "Adds the starting lab test catalogue (activity, specimen and observation "
        "definitions, linked to the v11 tariff) for a facility, or for every facility "
        "with a unit code. Run setup_billing first. Safe to run again: tests that "
        "already exist are left as the admin set them. With --if-empty (run on "
        "startup), only facilities with no lab tests at all are seeded, and a "
        "facility that can't be seeded is reported instead of failing."
    )

    def add_arguments(self, parser):
        parser.add_argument("--facility", help="Facility id or external id")
        parser.add_argument(
            "--if-empty",
            action="store_true",
            help="Only seed facilities that have no lab tests yet; never fail startup",
        )

    def handle(self, *args, **options):
        if_empty = options["if_empty"]
        if options["facility"]:
            value = options["facility"]
            lookup = {"id": value} if value.isdigit() else {"external_id": value}
            facilities = list(Facility.objects.filter(**lookup))
            if not facilities:
                raise CommandError(f"No facility {value}")
        else:
            facilities = list(
                Facility.objects.filter(unit_code__isnull=False, deleted=False)
            )
            if not facilities:
                if if_empty:
                    self.stdout.write("No facility has a unit code yet; no lab tests seeded")
                    return
                raise CommandError("No facility has a unit code yet")
        for facility in facilities:
            if if_empty and has_lab_tests(facility):
                self.stdout.write(f"{facility.name}: has lab tests; left as they are")
                continue
            try:
                added = seed_lab_catalogue(facility, only_if_empty=if_empty)
            except Exception as error:
                if not if_empty:
                    raise
                self.stderr.write(f"{facility.name}: lab tests not seeded: {error}")
                continue
            note = "" if added else " (is billing set up? run setup_billing)"
            self.stdout.write(f"{facility.name}: {added} lab tests added{note}")
