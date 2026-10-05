#!/bin/sh
# One-shot verification entrypoint.
#
# 1. byte-compile every Python source ("build check"),
# 2. run the full test-suite,
# 3. run the API smoke test against the healthy api service.
#
# Exits non-zero on the first failure so Compose reports the conclusion.
set -eu

echo "== verify: byte-compile check =="
python -m compileall -q app tests scripts

echo "== verify: unit/integration tests =="
python -m pytest -q

echo "== verify: API smoke test against ${API_BASE} =="
python -m scripts.smoke_test

echo "== verify: ALL CHECKS PASSED =="
