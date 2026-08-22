# -*- coding: utf-8 -*-
"""生日文本解析。

供聊天记忆、主动引擎和陪伴面板共同使用：
- 指定月日（阿拉伯或中文数字，年份会被忽略）；
- 农历/公历标注（农历六月十八、正月初三、腊月廿三…）；
- 相对说法（明天/后天/大后天生日、过两天生日、N 天后生日、生日还有 N 天）。

本模块保持零第三方依赖，也不依赖 AstrBot，便于独立测试。
"""
from __future__ import annotations

import re

LUNAR_CALENDAR = "lunar"
SOLAR_CALENDAR = "solar"

_LUNAR_HINT_RE = re.compile(r"农历|阴历|舊历|旧历")
_SOLAR_HINT_RE = re.compile(r"公历|阳历|新历")

# 1999年6月18日 / 6月18日 / 6月18 / 06-18 / 6.18 / 6/18（年份只识别不使用）
_ARABIC_BIRTHDAY_RE = re.compile(
    r"(?:(?:19|20)\d{2}\s*年)?\s*(\d{1,2})\s*(?:月|[-/.])\s*(\d{1,2})\s*(?:日|号)?"
)

_CN_DIGITS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_MONTH_WORDS = {"正": 1, "冬": 11, "腊": 12}

_CN_BIRTHDAY_RE = re.compile(
    r"([正月腊冬一二三四五六七八九十]{1,2})月"
    r"(初[一二三四五六七八九十]?|二十[一二三四五六七八九]?|廿[一二三四五六七八九]?|三十|十[一二三四五六七八九]?|[一二三四五六七八九])"
    r"(?:日|号)?"
)

_RELATIVE_WORD_DAYS = {"今天": 0, "明天": 1, "后天": 2, "大后天": 3}
_CN_DAYS_PATTERN = r"(\d{1,2}|[一二两三四五六七八九十]{1,3})"
_RELATIVE_DAY_RES: tuple[tuple[re.Pattern[str], bool], ...] = (
    (re.compile(r"(?:再)?过" + _CN_DAYS_PATTERN + r"天(?:之|以)?后?(?:就|才|要)?是?(?:我?的?)?生日"), True),
    (re.compile(_CN_DAYS_PATTERN + r"天(?:之|以)?后(?:就|才|要)?是?(?:我?的?)?生日"), True),
    (re.compile(r"(?:距离?|离)?生日(?:还|再)?(?:有|过)" + _CN_DAYS_PATTERN + r"天"), True),
    (re.compile(r"(今天|明天|后天|大后天)(?:就|才|要)?是?(?:我?的?)?生日"), False),
)
_RELATIVE_WORD_ONLY_RE = re.compile(r"(今天|明天|后天|大后天)")


def _chinese_month(token: str) -> int | None:
    if not token:
        return None
    if token in _CN_MONTH_WORDS:
        return _CN_MONTH_WORDS[token]
    if token == "十一":
        return 11
    if token == "十二":
        return 12
    if len(token) == 1 and token in _CN_DIGITS:
        return _CN_DIGITS[token]
    return None


def _chinese_day(token: str) -> int | None:
    if not token:
        return None
    if token.startswith("初"):
        digit = _CN_DIGITS.get(token[1:2], 0) if len(token) > 1 else 1
        return digit if 1 <= digit <= 10 else None
    if token.startswith("廿"):
        digit = _CN_DIGITS.get(token[1:2], 0) if len(token) > 1 else 0
        day = 20 + digit
        return day if 20 <= day <= 29 else None
    if token == "十":
        return 10
    if token == "二十":
        return 20
    if token == "三十":
        return 30
    if "十" in token:
        left, _, right = token.partition("十")
        if left and left not in _CN_DIGITS:
            return None
        if right and right not in _CN_DIGITS:
            return None
        day = _CN_DIGITS.get(left, 1 if not left else 0) * 10 + _CN_DIGITS.get(right, 0)
        if left not in _CN_DIGITS and left:
            return None
        return day if 11 <= day <= 31 else None
    if len(token) == 1 and token in _CN_DIGITS:
        return _CN_DIGITS[token]
    return None


def _resolve_calendar(text: str, lunar_hint: bool) -> str:
    solar_hint = bool(_SOLAR_HINT_RE.search(text))
    if solar_hint and not lunar_hint:
        return SOLAR_CALENDAR
    if lunar_hint:
        return LUNAR_CALENDAR
    return SOLAR_CALENDAR


def _valid_birthday(calendar: str, month: int, day: int) -> bool:
    if not 1 <= month <= 12:
        return False
    max_day = 30 if calendar == LUNAR_CALENDAR else 31
    return 1 <= day <= max_day


def parse_birthday_text(text: str) -> dict | None:
    """从短文本中解析生日月日，年份会被忽略。

    返回 ``{"calendar": "solar"|"lunar", "month": int, "day": int, "raw": str}``；
    无法识别时返回 ``None``。
    """
    cleaned = re.sub(r"\s+", "", str(text or ""))
    if not cleaned:
        return None
    lunar_hint = bool(_LUNAR_HINT_RE.search(cleaned))

    arabic = _ARABIC_BIRTHDAY_RE.search(cleaned)
    if arabic:
        month = int(arabic.group(1))
        day = int(arabic.group(2))
        calendar = _resolve_calendar(cleaned, lunar_hint)
        if _valid_birthday(calendar, month, day):
            return {"calendar": calendar, "month": month, "day": day, "raw": arabic.group(0)}

    chinese = _CN_BIRTHDAY_RE.search(cleaned)
    if chinese:
        month = _chinese_month(chinese.group(1))
        day = _chinese_day(chinese.group(2))
        if month and day:
            day_token = chinese.group(2)
            strong_lunar = chinese.group(1)[:1] in _CN_MONTH_WORDS or day_token.startswith(("初", "廿"))
            calendar = _resolve_calendar(cleaned, lunar_hint or strong_lunar)
            if _valid_birthday(calendar, month, day):
                return {"calendar": calendar, "month": month, "day": day, "raw": chinese.group(0)}
    return None


def parse_relative_birthday_days(text: str, *, require_birthday_token: bool = True) -> int | None:
    """解析“过两天生日 / 后天就是我生日 / 生日还有 N 天”这类相对说法。

    返回距今天数（0 表示今天）；不构成相对生日说法时返回 ``None``。
    ``require_birthday_token=False`` 用于 Bot 刚问过生日、对方只回答“后天”的场景。
    """
    cleaned = str(text or "")
    if not cleaned:
        return None
    if require_birthday_token and "生日" not in cleaned:
        return None
    days: int | None = None
    for pattern, _numeric in _RELATIVE_DAY_RES:
        match = pattern.search(cleaned)
        if not match:
            continue
        token = match.group(1)
        if token in _RELATIVE_WORD_DAYS:
            days = _RELATIVE_WORD_DAYS[token]
        elif token.isdigit():
            days = int(token)
        else:
            days = _chinese_day(token)
        if days is None:
            continue
        if 0 <= days <= 60:
            return days
        return None
    if not require_birthday_token:
        word = _RELATIVE_WORD_ONLY_RE.search(cleaned)
        if word:
            days = _RELATIVE_WORD_DAYS.get(word.group(1))
            if days is not None:
                return days
    return None


def extract_labeled_birthday_line(text: str, label: str) -> str:
    """提取“标签：值”行的值部分，找不到时返回空字符串。"""
    match = re.search(r"(?m)^\s*" + re.escape(label) + r"\s*[：:]\s*(\S.*)$", str(text or ""))
    return match.group(1).strip() if match else ""
