#!/usr/bin/env bash
# Usage: ./validate.sh /path/to/dogwood-repo
# Builds the official Dogwood CLI (needs network access to crates.io) and validates
# policies.dw against schema.cedarschema. Exit code 0 = accepted, 2 = rejected.
set -euo pipefail
REPO="${1:?path to a clone of github.com/dogwood-policy/dogwood}"
HERE="$(cd "$(dirname "$0")" && pwd)"
cargo build --release --manifest-path "$REPO/dogwood-cli/Cargo.toml" --target-dir "$HERE/.target"
"$HERE/.target/release/dogwood" validate "$HERE/policies.dw" --policy-schema "$HERE/schema.cedarschema"
