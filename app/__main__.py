"""Service entrypoint: ``python -m app``.

Configuration via environment:
* ``HOST`` (default ``0.0.0.0``)
* ``PORT`` (default ``8080``)
* ``RULES_PATH`` (default ``fixtures/rules.json`` relative to the repo)
"""

from __future__ import annotations

import os
import sys

from .rules import RulesLoadError, load_default_rules
from .server import make_server


def main() -> int:
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))
    try:
        rules = load_default_rules()
    except RulesLoadError as exc:
        sys.stderr.write(f"fatal: cannot load rules fixture: {exc}\n")
        return 78  # EX_CONFIG
    httpd = make_server(host, port, rules=rules)
    sys.stderr.write(
        f"embodied-lab scheduler listening on http://{host}:{port} "
        f"(rules {rules.version} from {rules.source_path})\n"
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("shutting down\n")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
