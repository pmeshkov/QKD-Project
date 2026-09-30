"""Notebook compatibility import; active calibration logic lives with the app."""
from pathlib import Path
import sys

_controls = Path(__file__).resolve().parents[1] / "pulsed_GUI"
if str(_controls) not in sys.path:
    sys.path.insert(0, str(_controls))
from eom_calibration_core import *  # Preserve the notebook's existing public API.
