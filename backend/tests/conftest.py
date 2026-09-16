"""Test-wide isolation from the developer's local database and API credentials."""

import os


os.environ["LEAD_FINDER_DATABASE_URL"] = "sqlite://"
os.environ["LEAD_FINDER_FRANCE_TRAVAIL_CLIENT_ID"] = ""
os.environ["LEAD_FINDER_FRANCE_TRAVAIL_CLIENT_SECRET"] = ""
