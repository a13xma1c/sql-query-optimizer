"""Entry point for the frozen (PyInstaller) build - ``sqlanalyzer.main`` behind a plain script.

PyInstaller needs a script, not a console-script entry point, and the bootloader's ``sys.argv``
is passed through unchanged so ``--version`` / ``--self-check`` work on the bundle too.
"""

from sqlanalyzer import main

if __name__ == "__main__":
    main()
