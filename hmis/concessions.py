"""Desk concession choices and the highest eligible fixed discount (PTS-13)."""

# Approved manual concessions have no configured fixed percentage yet.
CONCESSIONS = {
    "Senior citizen — 15%": (15, 60),
    "Freedom fighter — 25%": (25, 70),
    "Defence veteran — 10%": (10, 18),
    "EWS / charity": (0, 0),
    "Hospital discretionary": (0, 0),
}
APPROVED_CONCESSIONS = ("EWS / charity", "Hospital discretionary")


def highest_concession(values, age):
    eligible = [
        value
        for value in values
        if value in CONCESSIONS and age >= CONCESSIONS[value][1]
    ]
    return max(eligible, key=lambda value: CONCESSIONS[value][0], default="")
