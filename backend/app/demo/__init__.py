"""Deterministic demo data.

The dataset a developer seeds locally, and the loader that writes it. Nothing
here runs at application start-up: demo records appear because somebody asked
for them, never because the process booted.
"""

from app.demo.dataset import DEMO_ORGANIZATIONS, GLOBEX, NORTHWIND, demo_id
from app.demo.seed import seed_demo_data

__all__ = ["DEMO_ORGANIZATIONS", "GLOBEX", "NORTHWIND", "demo_id", "seed_demo_data"]
