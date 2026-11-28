#!/bin/sh
# Build the Ink UI into a private folder and run only the tests whose name matches $1.
# Safe to run in parallel: every call compiles into its own ui/build/check.* folder.
set -e
cd "$(dirname "$0")/../ui"
[ -d node_modules ] || npm ci --silent
mkdir -p build
out=$(mktemp -d build/check.XXXXXX)
trap 'rm -rf "$out"' EXIT
npx tsc -p . --outDir "$out"
node --test --test-name-pattern="$1" "$out"/test/*.test.js > "$out/report" 2>&1 || { cat "$out/report"; exit 1; }
cat "$out/report"
# a pattern that matches no test must not pass
grep -v "$out" "$out/report" | grep -Eq "^✔ .*($1)" || { echo "no test matched: $1"; exit 1; }
