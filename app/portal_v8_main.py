#!/usr/bin/env python3
from __future__ import annotations

import os

# Ajustes de entorno (importantes para Pi)
os.environ.setdefault("TZ", "America/Mexico_City")
os.environ.setdefault("OMP_NUM_THREADS", "2")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

from app.portal_v8_app import app  # noqa


def main():
    try:
        # Producción (recomendado)
        from waitress import serve
        serve(app, host="0.0.0.0", port=5000, threads=12)
    except Exception:
        # Fallback dev
        app.run(host="0.0.0.0", port=5000, threaded=True)


if __name__ == "__main__":
    main()
