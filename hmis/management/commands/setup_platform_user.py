from django.core.management.base import BaseCommand

from hmis.platform_admin import setup_platform_user


class Command(BaseCommand):
    help = (
        "Creates the IHL config portal's service account (ihl-platform), with no "
        "roles. Safe to run again. Then create its key in Settings → API keys "
        'with scope "platform_admin" and paste it into the portal.'
    )

    def handle(self, *args, **options):
        user, created = setup_platform_user()
        state = "created" if created else "already set up"
        self.stdout.write(self.style.SUCCESS(f"{user.username} {state}."))
