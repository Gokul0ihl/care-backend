from django.core.management.base import BaseCommand

from hmis.opd_vitals import seed


class Command(BaseCommand):
    help = (
        "Creates or refreshes the system-owned OPD vitals questionnaire (slug "
        "opd-vitals) and its global observation definitions (codes, units and "
        "adult reference ranges) from the catalogue in hmis/opd_vitals.py. "
        "Dry run by default: prints what it would change. Pass --apply to write. "
        "Safe to run again; edits made in the form builder are overwritten."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Write the changes")

    def handle(self, *args, **options):
        apply = options["apply"]
        for line in seed(apply=apply):
            self.stdout.write(line)
        self.stdout.write("Applied." if apply else "Dry run; pass --apply to write.")
