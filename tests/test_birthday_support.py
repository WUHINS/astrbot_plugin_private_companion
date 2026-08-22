# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from astrbot_plugin_private_companion.birthday_support import (
    parse_birthday_text,
    parse_relative_birthday_days,
)
from astrbot_plugin_private_companion.proactive import ProactiveMixin
from astrbot_plugin_private_companion.proactive_engine import ProactiveEngineMixin
from astrbot_plugin_private_companion.user_memory import UserMemoryMixin


class BirthdayParseTests(unittest.TestCase):
    def test_arabic_forms_ignore_year(self) -> None:
        for text, month, day in [
            ("6月18日", 6, 18),
            ("6月18", 6, 18),
            ("1999年6月18日", 6, 18),
            ("06-18", 6, 18),
            ("6.18", 6, 18),
            ("6/18", 6, 18),
        ]:
            profile = parse_birthday_text(text)
            self.assertIsNotNone(profile, text)
            assert profile is not None
            self.assertEqual("solar", profile["calendar"], text)
            self.assertEqual(month, profile["month"], text)
            self.assertEqual(day, profile["day"], text)

    def test_lunar_prefix_and_chinese_numerals(self) -> None:
        cases = [
            ("农历6月18", "lunar", 6, 18),
            ("农历六月初三", "lunar", 6, 3),
            ("正月初一", "lunar", 1, 1),
            ("腊月廿三", "lunar", 12, 23),
            ("十一月二十", "solar", 11, 20),
            ("六月十八", "solar", 6, 18),
            ("公历六月十八", "solar", 6, 18),
            ("十二月三十", "solar", 12, 30),
        ]
        for text, calendar, month, day in cases:
            profile = parse_birthday_text(text)
            self.assertIsNotNone(profile, text)
            assert profile is not None
            self.assertEqual(calendar, profile["calendar"], text)
            self.assertEqual(month, profile["month"], text)
            self.assertEqual(day, profile["day"], text)

    def test_invalid_values_return_none(self) -> None:
        for text in ("13月5日", "6月32日", "不适用", "", "下周吧"):
            self.assertIsNone(parse_birthday_text(text), text)

    def test_relative_expressions(self) -> None:
        cases = [
            ("我明天生日", 1),
            ("后天就是我生日", 2),
            ("我大后天生日", 3),
            ("我过两天生日", 2),
            ("再过三天就是我生日", 3),
            ("生日还有5天", 5),
            ("距离生日还有10天", 10),
            ("7天后就是我生日", 7),
            ("十天后生日", 10),
            ("过两天就是我生日啦", 2),
        ]
        for text, days in cases:
            self.assertEqual(days, parse_relative_birthday_days(text), text)

    def test_relative_requires_birthday_token_by_default(self) -> None:
        self.assertIsNone(parse_relative_birthday_days("后天"))
        self.assertEqual(2, parse_relative_birthday_days("后天", require_birthday_token=False))

    def test_relative_negative_or_plain_text(self) -> None:
        for text in ("别问生日", "上次生日很开心", "生日快乐", "不想说生日"):
            self.assertIsNone(parse_relative_birthday_days(text), text)


class _BirthdayHarness(UserMemoryMixin, ProactiveMixin, ProactiveEngineMixin):
    def __init__(self) -> None:
        self.data = {"important_dates": []}
        self.enable_companion_memory = True
        self.max_companion_memory_items = 200
        self.schedule_persona_prompt = ""
        self.roleplay_user_profile_prompt = ""

    @staticmethod
    def _environment_now() -> datetime:
        return datetime(2026, 8, 22, 12, 0)

    @staticmethod
    def _environment_fromtimestamp(value: float) -> datetime:
        return datetime.fromtimestamp(value)

    @staticmethod
    def _private_user_role(_user, _user_id="") -> str:
        return "owner"

    @staticmethod
    def _latest_private_user_activity_ts(user) -> float:
        return float(user.get("last_activity_at") or 0)

    @staticmethod
    def _photo_text_available(_user=None) -> bool:
        return False


class UserMemoryBirthdayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = _BirthdayHarness()

    def test_explicit_chinese_numeral_birthday_recorded(self) -> None:
        user: dict = {}
        self.harness._update_companion_memory_from_message(user, "我的生日是农历六月初三")
        profile = user.get("birthday_profile")
        self.assertIsNotNone(profile)
        assert profile is not None
        self.assertEqual("lunar", profile["calendar"])
        self.assertEqual(6, profile["month"])
        self.assertEqual(3, profile["day"])
        self.assertEqual("user_explicit", profile["source"])

    def test_relative_birthday_converted_to_date(self) -> None:
        user: dict = {}
        self.harness._update_companion_memory_from_message(user, "我过两天生日")
        profile = user.get("birthday_profile")
        self.assertIsNotNone(profile)
        assert profile is not None
        target = self.harness._environment_now() + timedelta(days=2)
        self.assertEqual("solar", profile["calendar"])
        self.assertEqual(target.month, profile["month"])
        self.assertEqual(target.day, profile["day"])
        self.assertEqual(2, profile.get("relative_days"))

    def test_answer_after_curiosity_question_without_birthday_token(self) -> None:
        user: dict = {"birthday_curiosity_asked_at": datetime(2026, 8, 22, 8, 0).timestamp()}
        self.harness._update_companion_memory_from_message(user, "后天")
        profile = user.get("birthday_profile")
        self.assertIsNotNone(profile)
        assert profile is not None
        target = self.harness._environment_now() + timedelta(days=2)
        self.assertEqual(target.month, profile["month"])
        self.assertEqual("birthday_curiosity_reply", profile["source"])
        self.assertEqual(0, user.get("birthday_curiosity_asked_at"))

    def test_plain_activity_not_recorded_as_birthday(self) -> None:
        user: dict = {}
        self.harness._update_companion_memory_from_message(user, "今天天气不错")
        self.assertNotIn("birthday_profile", user)


class ProactiveBirthdayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = _BirthdayHarness()

    @staticmethod
    def _ts(year: int, month: int, day: int, hour: int = 12) -> float:
        return datetime(year, month, day, hour).timestamp()

    def test_configured_user_birthday_fallback_matches(self) -> None:
        self.harness.roleplay_user_profile_prompt = "对用户的称呼：小林\n用户生日：农历五月初五"
        self.assertEqual(
            {"calendar": "lunar", "month": 5, "day": 5},
            self.harness._configured_user_birthday_profile(),
        )
        user: dict = {}
        matched = False
        for offset in range(-370, 371):
            day = datetime(2026, 1, 1) + timedelta(days=offset)
            if self.harness._birthday_profile_matches_on_date(user, day):
                matched = True
                break
        self.assertTrue(matched)

    def test_chat_profile_takes_priority_over_config(self) -> None:
        self.harness.roleplay_user_profile_prompt = "用户生日：1月1日"
        user = {"birthday_profile": {"calendar": "solar", "month": 8, "day": 22}}
        self.assertTrue(self.harness._birthday_profile_matches_on_date(user, datetime(2026, 8, 22)))
        self.assertFalse(self.harness._birthday_profile_matches_on_date(user, datetime(2026, 1, 1)))

    def test_curiosity_skipped_when_configured(self) -> None:
        self.harness.roleplay_user_profile_prompt = "用户生日：6月18日"
        self.assertTrue(self.harness._birthday_curiosity_has_known_birthday({}))

    def test_bot_birthday_event_on_matching_day(self) -> None:
        self.harness.schedule_persona_prompt = "姓名：微光\n生日：农历六月初三"
        # 农历六月初三在 2026 年对应公历 2026-07-16（用引擎换算验证，而不是硬编码）。
        target = None
        for offset in range(0, 200):
            day = datetime(2026, 7, 1) + timedelta(days=offset)
            if self.harness._bot_birthday_profile() and self.harness._birthday_profile_matches_profile(
                self.harness._bot_birthday_profile(), day
            ):
                target = day
                break
        self.assertIsNotNone(target)
        assert target is not None
        now = target.replace(hour=14).timestamp()
        user = {"last_activity_at": now - 3600, "ignored_streak": 0}
        event = self.harness._pick_bot_birthday_event(user, now)
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual("bot_birthday_share", event["reason"])
        self.assertEqual(2026, event["context"]["observance_year"])

        user["bot_birthday_event"] = {"shared_year": 2026}
        self.assertIsNone(self.harness._pick_bot_birthday_event(user, now))

    def test_bot_birthday_requires_persona_date_and_activity(self) -> None:
        now = self._ts(2026, 8, 22)
        self.assertIsNone(self.harness._pick_bot_birthday_event({"last_activity_at": now - 60}, now))
        self.harness.schedule_persona_prompt = "生日：8月22日"
        self.assertIsNone(self.harness._pick_bot_birthday_event({"last_activity_at": 0}, now))
        event = self.harness._pick_bot_birthday_event({"last_activity_at": now - 3600}, now)
        self.assertIsNotNone(event)
        self.assertIsNone(self.harness._pick_bot_birthday_event({"last_activity_at": now - 3600}, self._ts(2026, 8, 23)))


if __name__ == "__main__":
    unittest.main()
