"""One-command data preparation: ``python prepare.py``.

Thin shim over ``acpl_assistant.prepare.cli`` so the brief's single command works without
knowing the package layout. Equivalent to the ``acpl-prepare`` console script.
"""

from acpl_assistant.prepare.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
