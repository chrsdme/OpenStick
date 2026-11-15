#!/bin/sh
# SPDX-License-Identifier: MIT
# Fetch external sources at pinned commits into src/ (git-ignored).
set -eu
cd "$(dirname "$0")/.."
grep -v '^#' config/src-pins.txt | while read -r url sha; do
    [ -n "$url" ] || continue
    d=src/$(basename "$url")
    if [ -d "$d" ] && [ -n "$(ls -A "$d")" ]; then echo "skip $d"; continue; fi
    mkdir -p "$d"
    git -C "$d" init -q; git -C "$d" remote add origin "$url"
    git -C "$d" fetch -q --depth 1 origin "$sha"; git -C "$d" checkout -q FETCH_HEAD
    echo "ok   $d @ $(echo "$sha" | cut -c1-8)"
done
