#!/usr/bin/env python3
"""Settings shared by the Python scripts, read from the environment.

This repo is public, so nothing here names a real host, path or person. Copy
config.example.env to .env.local (gitignored) and the scripts pick your values
up automatically; otherwise everything falls back to obvious placeholders that
will fail loudly rather than quietly acting on the wrong machine.
"""

import os

DEFAULTS = {
    "MUSIC_HOST": "music-server.local",
    "MUSIC_PATH": "/srv/music",
    "SUPERSEDED_PATH": "/srv/_superseded",
    "MB_CONTACT": "you@example.com",
    "NAVIDROME_CONTAINER": "navidrome-navidrome-1",
}


def _load_env_file():
    """Read .env.local into the process environment, without overriding it.

    Deliberately minimal: KEY=value lines, # comments, no quoting rules. Real
    values belong in this file, which git ignores.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env.local")
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


_load_env_file()


def get(name):
    return os.environ.get(name, DEFAULTS[name])


HOST = get("MUSIC_HOST")
MUSIC_PATH = get("MUSIC_PATH")
SUPERSEDED_PATH = get("SUPERSEDED_PATH")
CONTACT = get("MB_CONTACT")
NAVIDROME_CONTAINER = get("NAVIDROME_CONTAINER")
USER_AGENT = f"cd-importer/1.0 ( {CONTACT} )"


def is_placeholder(name):
    """True if a setting is still the repo default, so callers can warn."""
    return get(name) == DEFAULTS[name]
