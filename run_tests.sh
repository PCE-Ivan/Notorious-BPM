#!/bin/bash
# Runs every test file in its own process (never combined -- see
# test_library_manager.py's header for why), with HOME pointed at a throwaway
# directory. Every path the app derives from ~ (its config, library, logs,
# caches under ~/Library/Application Support) then lands in that scratch
# HOME if a test ever forgets to isolate itself, instead of in the real
# installation. (PYTHONUSERBASE keeps pip --user packages like mutagen
# importable despite the changed HOME.)
#
# Usage: ./run_tests.sh [test_file.py ...]
cd "$(dirname "${BASH_SOURCE[0]}")"
REAL_USER_BASE="$(python3 -c 'import site; print(site.USER_BASE)')"
files=("$@")
[ ${#files[@]} -eq 0 ] && files=(test_*.py)
failed=0
for f in "${files[@]}"; do
  scratch_home="$(mktemp -d)"
  printf "%-28s" "$f"
  out="$(HOME="$scratch_home" PYTHONUSERBASE="$REAL_USER_BASE" python3 "$f" 2>&1)"
  summary="$(echo "$out" | grep -E '^(OK|FAILED|Ran)' | tr '\n' ' ')"
  echo "$summary"
  if ! echo "$out" | grep -q '^OK'; then failed=1; echo "$out" | tail -20; fi
  # Anything the test wrote under the scratch HOME means it touched "real" paths.
  if [ -n "$(find "$scratch_home" -type f 2>/dev/null | head -1)" ]; then
    echo "  note: wrote files under ~ (should be isolated): $(find "$scratch_home" -type f | head -3 | tr '\n' ' ')"
  fi
  rm -rf "$scratch_home"
done
exit $failed
