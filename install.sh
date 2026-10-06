#!/bin/sh
set -eu

if [ "$#" -gt 1 ]; then
    printf '사용법: %s [설치 디렉터리]\n' "$0" >&2
    exit 1
fi
script_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
destination=${1:-"$HOME/.local/bin"}
for dependency in python3 rsync lsblk findmnt; do
    if ! command -v "$dependency" >/dev/null 2>&1; then
        printf '필요한 명령이 없습니다: %s\n' "$dependency" >&2
        exit 1
    fi
done
python3 -c 'import sys; sys.exit(sys.version_info < (3, 8))' || {
    printf 'Python 3.8 이상이 필요합니다.\n' >&2
    exit 1
}
mkdir -p -- "$destination"
if [ -e "$destination/42usb" ] || [ -L "$destination/42usb" ]; then
    printf '기존 파일을 덮어쓰지 않습니다: %s/42usb\n' "$destination" >&2
    exit 1
fi
install -m 755 -- "$script_dir/42usb.py" "$destination/42usb"
printf '설치 완료: %s/42usb\n' "$destination"
case ":$PATH:" in
    *":$destination:"*) ;;
    *) printf '해당 디렉터리를 PATH에 추가하세요. 기본 설치: export PATH="$HOME/.local/bin:$PATH"\n' ;;
esac
