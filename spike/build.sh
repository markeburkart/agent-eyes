#!/bin/sh
# Builds the camera probe with an embedded Info.plist (camera usage + Continuity Camera opt-in).
set -e
cd "$(dirname "$0")"
swiftc -O camprobe.swift -o camprobe \
  -Xlinker -sectcreate -Xlinker __TEXT -Xlinker __info_plist -Xlinker Info.plist
codesign -s - -f camprobe
