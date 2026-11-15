#!/bin/sh
# SPDX-License-Identifier: MIT
# Print-only rollback plan for board #6: Path A / GPT-T -> original stock. NEVER runs edl, dd, fastboot or sudo;
# it only reads the dump, CSV and GPT files and prints commands (see tools/rollback-plan.py for the logic).
# usage: tools/rollback-plan.sh --dump FILE --csv FILE --primary FILE --secondary FILE
#            [--work DIR] [--loader FILE] [--edl CMD] [--emmc SECTORS] [--dump-sha HEX] [--compare REREAD.bin]
# Exit 0 = every check passed and the plan was printed; non-zero = a check failed and NO command was printed.
# With --compare the exit code is 3 when some range differs (the repair commands for those ranges are printed).
exec python3 -I "$(dirname "$0")/rollback-plan.py" "$@"
