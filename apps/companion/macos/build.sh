#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUTPUT="${1:-$SCRIPT_DIR/build/DaemonHarnessCompanion}"
mkdir -p "$(dirname "$OUTPUT")"
XCODE_SWIFTC="/Applications/Xcode.app/Contents/Developer/Toolchains/XcodeDefault.xctoolchain/usr/bin/swiftc"
XCODE_SDK="/Applications/Xcode.app/Contents/Developer/Platforms/MacOSX.platform/Developer/SDKs/MacOSX.sdk"
MODULE_CACHE="${TMPDIR:-/tmp}/daemon-harness-companion-swift-cache"
if [[ -x "$XCODE_SWIFTC" && -d "$XCODE_SDK" ]]; then
  "$XCODE_SWIFTC" "$SCRIPT_DIR/main.swift" \
    -framework AppKit -sdk "$XCODE_SDK" -module-cache-path "$MODULE_CACHE" \
    -o "$OUTPUT"
else
  swiftc "$SCRIPT_DIR/main.swift" -framework AppKit \
    -module-cache-path "$MODULE_CACHE" -o "$OUTPUT"
fi
echo "$OUTPUT"
