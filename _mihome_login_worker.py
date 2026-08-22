# -*- coding: utf-8 -*-
from __future__ import annotations

import sys

from mijiaAPI import mijiaAPI


def main() -> int:
    if len(sys.argv) < 2:
        return 2
    try:
        mijiaAPI(sys.argv[1]).login()
        return 0
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
