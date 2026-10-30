#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TARGET="${MINIF2F_ROOT:-$ROOT/.datasets/miniF2F-lean4}"
MINIF2F_COMMIT="f0a20e14c1eeccd859d51bb4c2b3ee487889c303"

if [[ -e "$TARGET" ]]; then
  echo "Refusing to overwrite existing path: $TARGET" >&2
  exit 2
fi

mkdir -p "$(dirname "$TARGET")"
git clone https://github.com/google-deepmind/miniF2F.git "$TARGET"
git -C "$TARGET" checkout "$MINIF2F_COMMIT"
(
  cd "$TARGET"
  lake build
)

echo "miniF2F is ready at $TARGET"
