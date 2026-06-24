"""Theme of the week — a playful banner derived from the week's actual content.

Looks at the week's events, upcoming birthdays/holidays, and weather, then
picks a theme (title + emoji + gradient colors + a short blurb). Pure fun,
but grounded in what's actually happening.
"""

from __future__ import annotations

from datetime import datetime

SPORTS_WORDS = ("soccer", "baseball", "viva", "ignite", "spirit", "mystics",
                "practice", "game", "match", "tournament", "scrimmage")

# Only these birthdays are worth theming the whole week around. Acquaintance
# birthdays still show in the look-ahead, but they don't become the headline.
CLOSE_FAMILY = ("esther", "henry", "taylor")

# Holiday name fragment → (emoji, title, blurb, color1, color2)
_HOLIDAY_THEMES = {
    "juneteenth":     ("🎆", "Juneteenth Week", "Freedom, family, and a long weekend.", "#e52d27", "#b31217"),
    "independence":   ("🎆", "Star-Spangled Week", "Fireworks, cookouts, and red-white-blue.", "#3a47d5", "#ff3838"),
    "thanksgiving":   ("🦃", "Thanksgiving Week", "Gratitude, gravy, and good company.", "#c0392b", "#e67e22"),
    "christmas":      ("🎄", "Christmas Week", "Cozy lights and merry chaos.", "#11998e", "#c0392b"),
    "halloween":      ("🎃", "Spooky Season", "Costumes, candy, and a little mischief.", "#f0930b", "#6a0dad"),
    "new year":       ("🎉", "New Year Week", "Fresh starts and big plans.", "#8e2de2", "#4a00e0"),
    "valentine":      ("💝", "Valentine Week", "A little extra love this week.", "#ff5f6d", "#ffc371"),
    "mother":         ("💐", "Mother's Day Week", "Celebrate the moms.", "#ff758c", "#ff7eb3"),
    "father":         ("🎣", "Father's Day Week", "Celebrate the dads.", "#2c3e50", "#4ca1af"),
    "memorial":       ("🇺🇸", "Memorial Day Week", "Remembrance and the unofficial start of summer.", "#3a47d5", "#ff3838"),
    "labor":          ("🛠️", "Labor Day Week", "Last hurrah of summer.", "#373b44", "#4286f4"),
}

# Month → seasonal default (emoji, title, blurb, color1, color2)
_SEASONAL = {
    1:  ("❄️", "Deep Winter", "Bundle up and power through.", "#83a4d4", "#b6fbff"),
    2:  ("🌨️", "Late Winter", "Almost spring — hang in there.", "#6a85b6", "#bac8e0"),
    3:  ("🌱", "Early Spring", "Things are waking up.", "#56ab2f", "#a8e063"),
    4:  ("🌷", "Spring Bloom", "Green grass and full schedules.", "#f7971e", "#ffd200"),
    5:  ("🌸", "Merry May", "Sunshine and Saturday games.", "#56ab2f", "#a8e063"),
    6:  ("☀️", "Summer Kickoff", "Long days, packed weekends.", "#f7971e", "#ffd200"),
    7:  ("🏖️", "High Summer", "Heat, hydration, and hustle.", "#ff512f", "#f09819"),
    8:  ("🌻", "Late Summer", "Squeeze out every sunny minute.", "#f7971e", "#ff9a3c"),
    9:  ("🍂", "Early Fall", "Back to routines and crisp mornings.", "#d38312", "#a83279"),
    10: ("🍁", "Peak Autumn", "Sweaters, leaves, and fast weekends.", "#c0392b", "#e67e22"),
    11: ("🌧️", "Late Fall", "Gray skies, cozy plans.", "#616161", "#9bc5c3"),
    12: ("✨", "Holiday Stretch", "Festive and full.", "#11998e", "#c0392b"),
}


def pick_theme(week: list[dict], horizon: dict, weather: dict | None) -> dict:
    """Return a theme dict: {emoji, title, blurb, color1, color2}."""
    horizon = horizon or {}
    birthdays = [b for b in horizon.get("birthdays", []) if b.get("days_out", 99) <= 7]
    holidays_soon = [h for h in horizon.get("holidays", []) if h.get("days_out", 99) <= 7]

    titles = " ".join(
        (e.get("title", "") or "").lower()
        for day in week for e in day.get("events", [])
    )
    sports_count = sum(titles.count(w) for w in ("game", "match", "practice"))
    has_tournament = "tournament" in titles

    rainy_days = sum(
        1 for day in week
        if (day.get("weather") or {}).get("precip_pct", 0) >= 50
    )
    hot = any((day.get("weather") or {}).get("high_f", 0) >= 88 for day in week)

    # ── Priority ladder ──

    # 1. Holiday this week
    for h in holidays_soon:
        name = h.get("title", "").lower()
        for frag, theme in _HOLIDAY_THEMES.items():
            if frag in name:
                emoji, title, blurb, c1, c2 = theme
                return {"emoji": emoji, "title": title, "blurb": blurb, "color1": c1, "color2": c2}

    # 2. Tournament
    if has_tournament:
        return {"emoji": "🏆", "title": "Tournament Week",
                "blurb": "Bring the cooler, the chairs, and the snacks.",
                "color1": "#f7971e", "color2": "#ffd200"}

    # 3. Birthday(s) — ONLY for close family (Esther, Henry, Taylor).
    #    Acquaintance birthdays show in the look-ahead but never headline the week.
    close_bdays = [
        b for b in birthdays
        if any(n in (b.get("title", "") or "").lower() for n in CLOSE_FAMILY)
    ]
    if close_bdays:
        names = ", ".join(
            b["title"].replace("Birthday", "").replace("'s", "").replace("’s", "").strip()
            for b in close_bdays[:3]
        )
        return {"emoji": "🎂", "title": "Birthday Week",
                "blurb": f"Cake incoming — {names}." if names else "Family birthday this week!",
                "color1": "#ff758c", "color2": "#ff7eb3"}

    # 4. Sports-heavy
    if sports_count >= 4:
        return {"emoji": "⚽", "title": "Game On Week",
                "blurb": f"{sports_count} games & practices on deck.",
                "color1": "#11998e", "color2": "#38ef7d"}

    # 5. Rainy
    if rainy_days >= 3:
        return {"emoji": "☔", "title": "Rainy Stretch",
                "blurb": "Keep umbrellas handy — fields may be iffy.",
                "color1": "#4e9af1", "color2": "#6dd5ed"}

    # 6. Heat
    if hot:
        return {"emoji": "🥵", "title": "Heat Wave",
                "blurb": "Hydrate and seek shade out there.",
                "color1": "#ff512f", "color2": "#f09819"}

    # 7. Seasonal default
    emoji, title, blurb, c1, c2 = _SEASONAL.get(datetime.now().month, _SEASONAL[6])
    return {"emoji": emoji, "title": title, "blurb": blurb, "color1": c1, "color2": c2}
