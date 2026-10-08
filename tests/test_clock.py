from datetime import datetime, timezone
from unittest.mock import patch

import clock


class _FakeDT(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 10, 7, 23, 30, tzinfo=timezone.utc).astimezone(tz)


def test_today_uses_configured_zone():
    clock.configure("Europe/Madrid")
    with patch.object(clock, "datetime", _FakeDT):
        assert clock.today().isoformat() == "2026-10-08"  # 23:30 UTC = завтра в Madrid
    clock.configure("UTC")
    with patch.object(clock, "datetime", _FakeDT):
        assert clock.today().isoformat() == "2026-10-07"


def test_now_is_aware_in_zone():
    clock.configure("Europe/Madrid")
    n = clock.now()
    assert n.tzinfo is not None and n.utcoffset().total_seconds() in (3600, 7200)


def test_unconfigured_uses_local_zone():
    clock.configure(None)
    n = clock.now()
    assert n.tzinfo is not None
    assert n.utcoffset() == datetime.now().astimezone().utcoffset()
