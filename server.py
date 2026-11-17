"""Back-compat shim: `python server.py` / `python -m server` still run the bridge.
The code lives in clip_bridge.py (a unique top-level name, safe to pip-install)."""

from clip_bridge import main

if __name__ == "__main__":
    main()
