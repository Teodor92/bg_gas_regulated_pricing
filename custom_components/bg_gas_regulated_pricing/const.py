"""Constants for the Bulgarian Natural Gas Regulated Pricing integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "bg_gas_regulated_pricing"

CONF_REGION: Final = "region"
CONF_VAT_RATE: Final = "vat_rate"

DEFAULT_VAT_RATE: Final = 20.0

# Overgas publishes one PDF per licensed supply area, under a dated
# /wp-content/uploads/<YYYY>/<MM>/ path that changes every month. The slug is
# the stable part; the date part is discovered from the index page.
PRICE_INDEX_URL: Final = (
    "https://www.overgas.bg/za-overgaz/produkti-i-uslugi/tseni-na-prirodniya-gaz/"
)

# Bulgartransgaz, as transmission system operator, sets the representative
# gross calorific value used to convert metered volume into energy. Published
# no later than 15 days before the month it applies to.
GCV_INDEX_URL: Final = "https://bulgartransgaz.bg/pages/sertifikat-46.html"
GCV_BASE_URL: Final = "https://bulgartransgaz.bg/"

# "name" becomes the config entry title, and therefore the prefix of every
# entity id, so it is kept short. "area" is the full description shown as the
# device model and in the setup dropdown.
REGIONS: Final[dict[str, dict[str, str]]] = {
    "mrezhi": {
        "slug": "TSENA-SAJT_red",
        "name": "Overgas Mrezhi",
        "area": "Sofia and other Overgas Mrezhi areas",
    },
    "bansko_razlog": {
        "slug": "TSENA-SAJT_red-KPG-Bansko-i-Razlog",
        "name": "Overgas Bansko",
        "area": "Bansko and Razlog (CNG)",
    },
    "byala": {
        "slug": "TSENA-SAJT_red-KPG-Byala",
        "name": "Overgas Byala",
        "area": "Byala (CNG)",
    },
    "karnobat": {
        "slug": "TSENA-SAJT_red-KPG-Karnobat",
        "name": "Overgas Karnobat",
        "area": "Karnobat (CNG)",
    },
}

# Fixed conversion rate, irrevocably set for Bulgaria's euro adoption on
# 2026-01-01. Used only to cross-check the lev and euro tables against each
# other, never to convert a displayed price.
BGN_PER_EUR: Final = 1.95583

# Sanity bounds. A parse that falls outside these is rejected rather than
# published: a wrong price silently corrupts cumulative cost statistics in the
# Energy dashboard, which is far more expensive to undo than a missing update.
MIN_PRICE_EUR_MWH: Final = 20.0
MAX_PRICE_EUR_MWH: Final = 400.0
MIN_GCV_KWH_M3: Final = 9.0
MAX_GCV_KWH_M3: Final = 12.0

# Tolerance when checking that the components add up to the stated total, and
# that the lev table matches the euro table. Both tables are published rounded
# to the eurocent, so small residuals are expected.
SUM_TOLERANCE: Final = 0.03
FX_TOLERANCE: Final = 0.01

# Data older than this many days into a new month raises a repair issue.
STALE_AFTER_DAYS: Final = 3

ATTR_COMPONENTS: Final = "components"
ATTR_PERIOD: Final = "period"
ATTR_GCV: Final = "calorific_value"
ATTR_VAT_RATE: Final = "vat_rate"
ATTR_PRICE_SOURCE: Final = "price_source"
ATTR_GCV_SOURCE: Final = "calorific_value_source"
