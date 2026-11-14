"""test_scheduler.py — 调度对齐到本地时间网格的行为

调度器不再"跑完睡固定间隔"（那样每轮都会后移一个单轮耗时），而是以本地时间
当天 00:00 为网格起点，等到下一个 interval 的整数倍时刻再跑。
"""

import datetime as dt

import scheduler


class TestSecondsUntilNextSlot:
    def test_aligns_to_next_hour(self):
        now = dt.datetime(2026, 11, 14, 15, 20, 0)
        assert scheduler.seconds_until_next_slot(3600, now) == 2400

    def test_exactly_on_slot_waits_full_interval(self):
        now = dt.datetime(2026, 11, 14, 15, 0, 0)
        assert scheduler.seconds_until_next_slot(3600, now) == 3600

    def test_ten_minute_grid(self):
        now = dt.datetime(2026, 11, 14, 15, 7, 0)
        assert scheduler.seconds_until_next_slot(600, now) == 180

    def test_works_across_midnight(self):
        now = dt.datetime(2026, 11, 14, 23, 59, 30)
        assert scheduler.seconds_until_next_slot(3600, now) == 30

    def test_never_returns_zero(self):
        # 距离边界只剩 0.5 秒时也要等满 1 秒，避免同一时刻连跑两轮
        now = dt.datetime(2026, 11, 14, 15, 59, 59, 500_000)
        assert scheduler.seconds_until_next_slot(3600, now) == 1.0
