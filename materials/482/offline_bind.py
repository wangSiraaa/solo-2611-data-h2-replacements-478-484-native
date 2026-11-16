"""Standalone offline binding diagnostics (no database is ever opened).

Usage:
    python3 offline_bind.py catalog.json request.json

Exit code 0 prints the JSON BoundPlan; a non-zero exit prints the binding
path diagnostic. This demonstrates bind() works in a process that has no
database connection and only knows the catalog produced elsewhere.
"""
import json
import sys

from binding import BindingError, bind


def main(argv):
    if len(argv) != 3:
        print("usage: python3 offline_bind.py catalog.json request.json",
              file=sys.stderr)
        return 2
    with open(argv[1]) as catalog_file:
        catalog = json.load(catalog_file)
    with open(argv[2]) as request_file:
        request = json.load(request_file)
    try:
        plan = bind(catalog, request)
    except BindingError as exc:
        print(json.dumps({"rejected": True, "error": str(exc)},
                         sort_keys=True))
        return 1
    print(json.dumps(plan, sort_keys=True, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
