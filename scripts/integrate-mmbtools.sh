#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUNDLE_DIR="$ROOT_DIR/tools/bin"

mkdir -p "$BUNDLE_DIR"

copy_tool() {
    local tool_name="$1"
    local source_path
    source_path="$(command -v "$tool_name" || true)"
    if [[ -z "$source_path" ]]; then
        echo "outil absent du PATH: $tool_name" >&2
        return 1
    fi
    cp -L "$source_path" "$BUNDLE_DIR/$tool_name"
    chmod 755 "$BUNDLE_DIR/$tool_name"
    echo "copie: $tool_name -> $BUNDLE_DIR/$tool_name"
}

copy_tool "edi2eti"
copy_tool "odr-edi2edi"
copy_tool "eti2zmq"

echo
echo "Bundle local pret dans: $BUNDLE_DIR"
