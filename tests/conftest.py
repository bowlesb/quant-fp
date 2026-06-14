"""Shared test environment for the feature-platform suite.

``loaders.py`` reads ``DB_PASSWORD`` at import time (fail-fast for the real job), so any test that
imports a DB-touching module (e.g. ``validate``, ``parity``) needs it present merely to import. Tests
never actually connect — they exercise pure logic or a tmp parquet store — so a dummy default keeps the
suite importable without a live database. The real job always supplies the real credential.
"""
from __future__ import annotations

import os

os.environ.setdefault("DB_PASSWORD", "test")
