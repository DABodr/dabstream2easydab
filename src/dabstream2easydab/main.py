from __future__ import annotations

import sys


def main() -> int:
    from .gui import DABStreamApplication

    app = DABStreamApplication()
    return app.run(sys.argv)
