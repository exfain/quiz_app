from django.core.management.base import BaseCommand

from games_website.services import restore_all_models_from_supabase


class Command(BaseCommand):
    help = "Restore all local SQLite data from Supabase"

    def handle(self, *args, **options):
        """Delegate to the shared sync service."""
        total, models_restored = restore_all_models_from_supabase(stdout=self.stdout, stderr=self.stderr)
        self.stdout.write(
            self.style.SUCCESS(
                f"Restore completed via management command. Total items restored: {total}. "
                f"Models: {', '.join(models_restored) if models_restored else 'none'}"
            )
        )
        self.stdout.write(self.style.SUCCESS(f"Restored models: {models_restored}"))
