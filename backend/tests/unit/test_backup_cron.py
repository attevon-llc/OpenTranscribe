def test_max_interval_is_the_longest_gap_between_fires():
    from datetime import timedelta

    from app.services import backup_cron

    assert backup_cron.max_interval("0 3 * * *") == timedelta(days=1)
    assert backup_cron.max_interval("0 3 * * 0") == timedelta(days=7)
    assert backup_cron.max_interval("0 3 1,15 * *") >= timedelta(days=15)
