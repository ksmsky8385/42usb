#!/bin/sh
set -eu

script_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
if ! command -v python3 >/dev/null 2>&1; then
    printf 'Python 3.8 이상이 필요합니다.\n' >&2
    exit 1
fi
python3 -c 'import sys; sys.exit(sys.version_info < (3, 8))' || {
    printf 'Python 3.8 이상이 필요합니다.\n' >&2
    exit 1
}
exec python3 -B "$script_dir/install.py" "$@"
