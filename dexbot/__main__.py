import os

# Windows consoles default to a legacy code page. UTF-8 mode is a no-op elsewhere.
os.environ.setdefault("PYTHONUTF8", "1")

from dexbot.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
