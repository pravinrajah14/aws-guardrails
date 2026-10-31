#!/usr/bin/env bash
# Usage: ./validate.sh /path/to/dogwood-repo (building the CLI needs network access to crates.io)
set -euo pipefail
REPO="${1:?path to a clone of github.com/dogwood-policy/dogwood}"
HERE="$(cd "$(dirname "$0")" && pwd)"
cargo build --release --manifest-path "$REPO/dogwood-cli/Cargo.toml" --target-dir "$HERE/.target"
"$HERE/.target/release/dogwood" validate "$HERE/policies.dw" --policy-schema "$HERE/../kylie-test-bed-upload/policies/schema.cedarschema"
