#!/usr/bin/env bash
set -eu
printf 'FAKE BUILD STUB: %s\n' "${0##*/}"
exit "${OPENSTICK_FAKE_BUILD_EXIT:-0}"
