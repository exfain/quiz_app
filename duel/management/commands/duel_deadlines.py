import time
import logging
from django.core.management.base import BaseCommand
from django.db import close_old_connections
from duel.deadlines import reconcile_due_rounds


class Command(BaseCommand):
    help = "Reconcile persisted duel deadlines, independently of browsers."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--interval", type=float, default=0.5)

    def handle(self, *args, **options):
        if options["interval"] <= 0:
            from django.core.management.base import CommandError
            raise CommandError("interval must be positive")
        try:
            while True:
                close_old_connections()
                try:
                    count = reconcile_due_rounds()
                except Exception:
                    if options['once']:
                        raise
                    logging.getLogger(__name__).exception('Duell-Fristverarbeitung fehlgeschlagen; erneuter Versuch folgt.')
                    time.sleep(max(1, options['interval']))
                    continue
                if options["once"]:
                    self.stdout.write(str(count))
                    return
                time.sleep(options["interval"])
        except KeyboardInterrupt:
            return
