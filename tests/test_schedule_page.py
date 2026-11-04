"""The zone form's schedule page: a summary of the week at the top, then the weekday,
weekend and per-day changes."""
from __future__ import annotations

import json
from pathlib import Path

from custom_components.heating_manager.config_flow import week_summary

from .test_config_flow import configure, edit_zone, entry, env, period, shown, start_zone, zone_input  # noqa: F401

STRINGS = Path(__file__).resolve().parents[1] / "custom_components" / "heating_manager" / "translations" / "en.json"
WEEKDAY = [{"start": "06:00", "temperature": 20.0}, {"start": "22:30", "temperature": 16.0}]
WEEKEND = [{"start": "08:00", "temperature": 20.0}, {"start": "23:00", "temperature": 16.0}]


def test_days_alike_are_grouped():
    summary = week_summary({"weekday": WEEKDAY, "weekend": WEEKEND, "wednesday": [{"start": "07:00", "temperature": 19.5}]}, 10.0)
    assert summary.splitlines() == [
        "| Days | Temperature changes |",
        "|---|---|",
        "| Mon, Tue, Thu, Fri | 06:00 20° · 22:30 16° |",
        "| Wed *(own schedule)* | 07:00 19.5° |",
        "| Sat, Sun | 08:00 20° · 23:00 16° |",
    ]


def test_a_day_of_its_own_like_its_usual_one_is_still_marked():
    summary = week_summary({"weekday": WEEKDAY, "weekend": WEEKEND, "monday": WEEKDAY}, 10.0)
    assert "| Mon *(own schedule)* | 06:00 20° · 22:30 16° |" in summary
    assert "| Tue, Wed, Thu, Fri | 06:00 20° · 22:30 16° |" in summary


def test_a_day_with_no_changes():
    summary = week_summary({"weekday": WEEKDAY, "weekend": []}, 7.0)
    assert "| Sat, Sun | 7° all day (temperature outside schedule) |" in summary


async def schedule_page(hass, entry, zone_id=None, **values):
    result = await start_zone(hass, entry, zone_id)
    page = shown(result) if zone_id else zone_input(**values)
    return await configure(hass, result, {k: v for k, v in page.items() if k not in ("weekday", "weekend", "days")})


async def test_reconfigure_shows_the_saved_week(hass, entry):
    await edit_zone(hass, entry, days={"wednesday": [period("07:00", 19.5)]})
    result = await schedule_page(hass, entry, "downstairs")
    assert result["step_id"] == "schedule"
    placeholders = result["description_placeholders"]
    assert placeholders["week_title"] == "The week as saved:"
    assert "| Wed *(own schedule)* | 07:00 19.5° |" in placeholders["week"]
    assert "| Mon, Tue, Thu, Fri, Sat, Sun | 06:30 19° · 22:00 7° |" in placeholders["week"]


async def test_a_new_zone_shows_its_default_week(hass, entry):
    result = await schedule_page(hass, entry, name="Upstairs")
    placeholders = result["description_placeholders"]
    assert placeholders["week_title"] == "A new zone starts with:"
    assert "| Mon, Tue, Wed, Thu, Fri, Sat, Sun | 06:30 19° · 22:00 7° |" in placeholders["week"]


async def test_schedule_page_is_translated(hass, entry):
    strings = json.loads(STRINGS.read_text())["config_subentries"]["zone"]["step"]["schedule"]
    assert strings["title"]
    assert "{week}" in strings["description"] and "{week_title}" in strings["description"]
    result = await schedule_page(hass, entry, "downstairs")
    for key, section in result["data_schema"].schema.items():
        names = strings["sections"][str(key)]
        assert names["name"]
        for inner in section.schema.schema:
            assert str(inner) in names["data"], (key, inner)
