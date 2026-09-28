"""Open QKD experiment controls. Opening the window does not start hardware."""
from pathlib import Path
import sys


def main():
    # Keep existing direct-script imports and saved GUI settings compatible.
    application_directory = Path(__file__).resolve().parent / "pulsed_GUI"
    sys.path.insert(0, str(application_directory))
    from gui_app import launch

    launch()


if __name__ == "__main__":
    main()
