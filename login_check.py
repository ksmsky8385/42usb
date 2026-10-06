#!/usr/bin/env python3
"""One-shot graphical login check. Never moves data or checks for updates."""
import argparse
import html
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

sys.dont_write_bytecode = True


def load_core():
    spec = importlib.util.spec_from_file_location('fortytwo_usb_core', Path(__file__).with_name('42usb.py'))
    core = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(core)
    return core


def check_links(core, home):
    links, errors = core.home_links(home)
    if errors:
        print(f'42_usb: 읽지 못한 홈 경로 {len(errors)}개', file=sys.stderr)
    if not links:
        return []
    try:
        registry = core.read_registry(home / '.42usb-state')
    except (core.Error, OSError, ValueError) as exc:
        # Existing manual links can still be inspected without a valid registry.
        print(f'42_usb: 이동 기록을 읽지 못했습니다: {exc}', file=sys.stderr)
        registry = {}
    _, volumes = core.usb_devices()
    mounted = {}
    for volume in volumes:
        for location in volume.get('mountpoints', []):
            if not location:
                continue
            mount = Path(location)
            if not any(core.beneath(target, mount) for _, target in links):
                continue
            try:
                core.mount_info(volume, mount)
                mounted[(volume['path'], str(mount))] = True
            except (core.Error, OSError, ValueError, KeyError):
                mounted[(volume['path'], str(mount))] = False
    unavailable = []
    for local, target in links:
        reason = core.link_status(local, target, registry, volumes, mounted)
        if not reason.startswith('정상:'):
            unavailable.append((local, reason))
    return unavailable


def warning_text(home, unavailable):
    lines = ['42_home 데이터가 있는 USB가 연결되어 있지 않거나,',
             '올바르게 마운트되지 않아 일부 데이터를 사용할 수 없습니다.', '',
             'USB를 연결하고 원래 경로에 마운트한 뒤 앱을 실행하세요.', '']
    for path, reason in unavailable[:5]:
        lines.append(f'• ~/{path.relative_to(home)}: {reason}')
    if len(unavailable) > 5:
        lines.append(f'외 {len(unavailable) - 5}개 경로')
    return '\n'.join(lines)


def show_warning(message):
    # A dialog is visible even when the desktop suppresses notification banners.
    if shutil.which('zenity'):
        try:
            result = subprocess.run(['zenity', '--warning', '--title=42_usb',
                                     '--no-markup', '--width=560', '--timeout=120',
                                     '--text=' + message], capture_output=True, text=True, timeout=130)
            if result.returncode in (0, 5) or (result.returncode == 1 and not result.stderr.strip()):
                return True
        except (OSError, subprocess.TimeoutExpired):
            pass
    if shutil.which('notify-send'):
        try:
            result = subprocess.run(['notify-send', '--app-name=42_usb', '--urgency=critical',
                                     '--icon=dialog-warning', '42_usb', html.escape(message)],
                                    capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                return True
        except (OSError, subprocess.TimeoutExpired):
            pass
    print('42_usb: 경고창·알림을 표시하지 못했습니다.\n' + message, file=sys.stderr)
    return False


def main(argv=None):
    parser = argparse.ArgumentParser(description='42_home USB 링크를 한 번 확인하고 경고합니다.')
    parser.add_argument('--delay', type=float, default=10,
                        help='로그인 자동 마운트 대기 시간(초, 기본 10)')
    parser.add_argument('--dry-run', action='store_true', help='창·알림 없이 결과만 출력')
    args = parser.parse_args(argv)
    if not 0 <= args.delay <= 60:
        parser.error('--delay는 0~60초여야 합니다.')
    if not args.dry_run and not (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')):
        print('42_usb: 그래픽 로그인 세션이 없어 경고창 검사를 건너뜁니다.', file=sys.stderr)
        return 0
    time.sleep(args.delay)
    core = load_core()
    home = Path.home()
    try:
        unavailable = check_links(core, home)
    except (core.Error, OSError, ValueError, KeyError) as exc:
        message = f'USB 상태를 확인하지 못했습니다.\n42usb status로 확인하세요.\n\n{exc}'
        if args.dry_run:
            print(message, file=sys.stderr)
        else:
            show_warning(message)
        return 1
    if not unavailable:
        if args.dry_run:
            print('42_home 링크가 없거나 모든 링크의 USB 마운트와 대상이 정상입니다.')
        return 0
    message = warning_text(home, unavailable)
    if args.dry_run:
        print(message)
        return 1
    return 0 if show_warning(message) else 1


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
