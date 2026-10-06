#!/usr/bin/env python3
"""Stable launcher; update logic lives in the selected release's updater.py."""
import fcntl
import importlib.util
import os
from pathlib import Path
import subprocess
import sys

INSTALL_ROOT = None  # Replaced with an absolute path by install.py.


def main():
    if INSTALL_ROOT is None:
        print('먼저 sh install.sh --repo REPOSITORY로 설치하세요.', file=sys.stderr)
        return 1
    root = Path(INSTALL_ROOT)
    if root.is_symlink():
        raise RuntimeError('설치 디렉터리가 심볼릭 링크로 변경되었습니다.')
    if sys.argv[1:] == ['--login-check']:
        # Resolve one immutable release; never fetch, prompt, or hold the data lock.
        release = (root / 'app').resolve(strict=True)
        if release.parent != (root / 'releases').resolve():
            raise RuntimeError('올바르지 않은 설치본 경로입니다.')
        return subprocess.run([sys.executable, '-B', str(release / 'login_check.py')]).returncode
    descriptor = os.open(root / 'run.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('다른 42usb 명령이 실행 중입니다. 완료 후 다시 실행하세요.', file=sys.stderr)
            return 1
        release = (root / 'app').resolve(strict=True)
        if release.parent != (root / 'releases').resolve():
            raise RuntimeError('올바르지 않은 설치본 경로입니다.')
        sys.dont_write_bytecode = True
        spec = importlib.util.spec_from_file_location('fortytwo_usb_updater', release / 'updater.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.run_cli(root, sys.argv[1:])
    finally:
        os.close(descriptor)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print('\n42usb: 취소했습니다.', file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        print(f'42usb: {exc}', file=sys.stderr)
        sys.exit(1)
