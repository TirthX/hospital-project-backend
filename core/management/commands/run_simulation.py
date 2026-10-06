"""
Run the hospital simulation engine on a timer.

The frontend also drives the simulation while a dashboard is open, but that
stops the moment the browser tab closes. This command keeps the hospital
live independently, which is useful for letting data build up before a
demonstration:

    python manage.py run_simulation                  # a tick every 8 seconds
    python manage.py run_simulation --interval 30    # slower
    python manage.py run_simulation --ticks 50       # run 50 ticks and stop
"""
import time

from django.core.management.base import BaseCommand
from django.utils import timezone

from core.simulation import simulation_tick


class Command(BaseCommand):
    help = 'Run the hospital simulation engine continuously.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--interval', type=float, default=8.0,
            help='Seconds between ticks (default: 8).',
        )
        parser.add_argument(
            '--ticks', type=int, default=0,
            help='Number of ticks to run, or 0 to run until interrupted.',
        )
        parser.add_argument(
            '--quiet', action='store_true',
            help='Print one summary line per tick instead of every event.',
        )

    def handle(self, *args, **options):
        interval = max(0.5, options['interval'])
        limit = options['ticks']
        quiet = options['quiet']

        self.stdout.write(self.style.SUCCESS(
            f'Simulation engine started — a tick every {interval:g}s'
            + (f' for {limit} ticks.' if limit else '. Press Ctrl+C to stop.')
        ))

        tick = 0
        try:
            while limit == 0 or tick < limit:
                tick += 1
                started = time.perf_counter()

                try:
                    result = simulation_tick()
                except Exception as exc:                  # noqa: BLE001
                    # One bad tick must not kill a long-running demo feed.
                    self.stderr.write(self.style.ERROR(
                        f'Tick {tick} failed: {exc}'
                    ))
                    time.sleep(interval)
                    continue

                elapsed_ms = (time.perf_counter() - started) * 1000
                stamp = timezone.localtime().strftime('%H:%M:%S')

                self.stdout.write(
                    f'[{stamp}] tick {tick:>4} '
                    f'({elapsed_ms:5.0f} ms)  '
                    f"+{result['new_admissions']} admitted  "
                    f"-{result['discharges']} discharged  "
                    f"{result['alerts_raised']} alerts  "
                    f"{result['total_active_patients']} active  "
                    f"{result['available_beds']} beds free"
                )

                if not quiet:
                    for event in result['events']:
                        self.stdout.write(f'           · {event}')

                if limit == 0 or tick < limit:
                    time.sleep(interval)

        except KeyboardInterrupt:
            self.stdout.write(self.style.WARNING(
                f'\nStopped after {tick} ticks.'
            ))
            return

        self.stdout.write(self.style.SUCCESS(f'Completed {tick} ticks.'))
