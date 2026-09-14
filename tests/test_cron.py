from datetime import datetime, timedelta, timezone
import unittest

from dagrunner.service import linux_cron_trigger


class LinuxCronTests(unittest.TestCase):
    def fire_times(self, weekday):
        trigger = linux_cron_trigger(f"40 8,23 * * {weekday}", "UTC")
        now = datetime(2026, 9, 14, tzinfo=timezone.utc)  # Monday
        end = now + timedelta(days=7)
        times = []
        previous = None
        while True:
            next_time = trigger.get_next_fire_time(previous, now)
            if next_time >= end:
                return times
            times.append(next_time)
            previous = next_time
            now = next_time + timedelta(seconds=1)

    def test_linux_numbers_and_names_fire_monday_through_saturday(self):
        expected = [
            datetime(2026, 9, day, hour, 40, tzinfo=timezone.utc)
            for day in range(14, 20) for hour in (8, 23)
        ]
        for value in ("1-6", "mon-sat", "MON-SAT"):
            with self.subTest(value=value):
                self.assertEqual(self.fire_times(value), expected)

    def test_sunday_aliases(self):
        for value in ("0", "7", "sun", "0,7"):
            with self.subTest(value=value):
                self.assertEqual([t.weekday() for t in self.fire_times(value)], [6, 6])

    def test_ranges_lists_and_steps(self):
        for value, days in (("0-7", set(range(7))), ("*", set(range(7))),
                            ("1-7/2", {0, 2, 4, 6}), ("mon,wed,6", {0, 2, 5}),
                            ("sun-sat", set(range(7))), ("*/2", {1, 3, 5, 6})):
            with self.subTest(value=value):
                self.assertEqual({t.weekday() for t in self.fire_times(value)}, days)

    def test_invalid_weekdays_are_rejected(self):
        for value in ("8", "-1", "6-1", "mon-nope", "*/0", "1,,2", "1/2"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                linux_cron_trigger(f"40 8,23 * * {value}", "UTC")


if __name__ == "__main__":
    unittest.main()
