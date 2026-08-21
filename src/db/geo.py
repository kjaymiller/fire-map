"""Shared great-circle distance helper.

Split out of notify.py so subscribers.py (ephemeral-subscription expiry)
can use the same math without notify.py and subscribers.py importing each
other -- notify.py already imports subscribers.py to read the subscriber
table.
"""

import math

EARTH_RADIUS_MILES = 3958.8


def distance_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points, in miles (haversine)."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * math.asin(math.sqrt(a))
