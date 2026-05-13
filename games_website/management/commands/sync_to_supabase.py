from django.core.management.base import BaseCommand

from games_website.services import sync_all_models_to_supabase


class Command(BaseCommand):
    help = "Sync all local SQLite data to Supabase"

    def handle(self, *args, **options):
        """Delegate to the shared sync service."""
        total_synced, synced_models = sync_all_models_to_supabase(stdout=self.stdout, stderr=self.stderr)
        self.stdout.write(
            self.style.SUCCESS(
                f"Sync completed via management command. Total items synced: {total_synced}. "
                f"Models: {', '.join(synced_models) if synced_models else 'none'}"
            )
        )
        self.stdout.write(self.style.SUCCESS(f"Synced models: {synced_models}"))
