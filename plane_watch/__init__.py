"""plane_watch: find aircraft in trouble and render analysis cards.

Combines ADS-B flight tracking (emergency squawks 7500/7600/7700 and the
transponder emergency status) with ACARS messages (bomb threats, diversions,
medical, security, ...) and draws a map card for each flagged aircraft.
"""

__version__ = "1.0.0"
