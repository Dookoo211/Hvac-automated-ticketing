#!/usr/bin/env python3
"""UI launcher entrypoint for packaged/windowed builds."""

from __future__ import annotations

import sys

from hvac_email_agent import main


if __name__ == "__main__":
    if "--ui" not in sys.argv:
        sys.argv.append("--ui")
    main()
