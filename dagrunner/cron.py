"""Cron expression helpers shared by scheduling, imports and tools."""
from __future__ import annotations

import re

from apscheduler.triggers.cron import CronTrigger


def linux_cron_trigger(expression: str, timezone: str) -> CronTrigger:
    """Parse five fields with Linux weekday numbering (0/7=Sunday)."""
    fields = expression.split()
    if len(fields) != 5:
        raise ValueError(f"Wrong number of fields; got {len(fields)}, expected 5")
    names = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")
    weekdays: set[int] = set()

    def weekday(value: str) -> int:
        if value in names:
            return names.index(value)
        if re.fullmatch(r"[0-7]", value):
            return int(value)
        raise ValueError(f"Invalid cron weekday: {value!r}")

    for item in fields[4].lower().split(","):
        match = re.fullmatch(r"(\*|[a-z]+|[0-7])(?:-([a-z]+|[0-7]))?(?:/([0-9]+))?", item)
        if not match:
            raise ValueError(f"Invalid cron weekday: {item!r}")
        first, last, stride = match.groups()
        step = int(stride) if stride else 1
        if step < 1:
            raise ValueError("Cron weekday step must be positive")
        if first == "*":
            if last is not None:
                raise ValueError(f"Invalid cron weekday range: {item!r}")
            start, end = 0, 7
        else:
            start = weekday(first)
            end = weekday(last) if last is not None else start
            if stride and last is None:
                raise ValueError("Cron weekday steps require a range or *")
        if start > end:
            raise ValueError(f"Invalid cron weekday range: {item!r}")
        weekdays.update(day % 7 for day in range(start, end + 1, step))
    fields[4] = ",".join(names[day] for day in sorted(weekdays))
    return CronTrigger.from_crontab(" ".join(fields), timezone=timezone)
