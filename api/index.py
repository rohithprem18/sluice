"""Vercel entry point: every /v1/* and /api/* request is routed here."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sluice.app import app  # noqa: E402,F401
