"""Run a Functions Framework target locally without Gunicorn or a reloader.

Google's CLI selects Gunicorn when debug mode is disabled and Werkzeug with a
reloader when it is enabled. Gunicorn's pre-fork model can crash Objective-C
libraries on macOS, while the reloader complicates coordinated shutdown. This
launcher creates the same Functions Framework Flask app and serves it in one
threaded development process.
"""
from __future__ import annotations

import argparse

from functions_framework import create_app
from werkzeug.serving import run_simple


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--signature-type", default="http")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()

    app = create_app(args.target, args.source, args.signature_type)
    run_simple(
        args.host,
        args.port,
        app,
        use_debugger=False,
        use_reloader=False,
        threaded=True,
    )


if __name__ == "__main__":
    main()
