#!/usr/bin/env python3
"""Install a pinned release and launcher without changing the development tree."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True
import updater


def desktop_entry(launcher):
    value = str(launcher)
    if '=' in value or any(ord(character) < 32 for character in value):
        raise updater.UpdateError('자동 실행 경로에는 등호나 제어 문자를 사용할 수 없습니다.')
    # Desktop Entry string escapes are decoded before Exec argument escapes.
    argument = ''.join('\\' + c if c in '\\"`$' else c for c in value)
    argument = argument.replace('\\', '\\\\').replace('%', '%%')
    try_exec = value.replace('\\', '\\\\')
    return ('[Desktop Entry]\nType=Application\nName=42_usb\n'
            'Comment=로그인 시 42_home USB 연결 및 마운트 확인\n'
            f'Exec="{argument}" --login-check\nTryExec={try_exec}\n'
            'Terminal=false\nNoDisplay=true\nStartupNotify=false\n'
            'X-GNOME-Autostart-enabled=true\n')


def publish_new(path, content, mode):
    """Publish one complete file without overwriting; clean up failed publication."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.42usb-install-', dir=path.parent)
    temporary = Path(temporary)
    linked = False
    try:
        with os.fdopen(descriptor, 'w') as stream:
            stream.write(content)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
        identity = temporary.stat()
        os.link(temporary, path)
        linked = True
        updater.sync_directory(path.parent)
        return path, identity.st_dev, identity.st_ino
    except BaseException:
        if linked and path.exists() and not path.is_symlink() and path.samefile(temporary):
            path.unlink()
        raise
    finally:
        temporary.unlink()


def main(argv=None):
    parser = argparse.ArgumentParser(description='Git 저장소를 고정해 42usb를 사용자 홈에 설치합니다.')
    parser.add_argument('--repo', help='GitHub URL; 생략하면 현재 프로젝트의 origin 사용')
    parser.add_argument('--branch', help='배포 브랜치; 생략하면 원격 기본 브랜치 사용')
    parser.add_argument('--bin-dir', type=Path, default=Path.home() / '.local/bin')
    parser.add_argument('--data-dir', type=Path, default=Path.home() / '.local/share/42usb')
    config_home = Path(os.environ.get('XDG_CONFIG_HOME', ''))
    if not config_home.is_absolute():
        config_home = Path.home() / '.config'
    parser.add_argument('--autostart-dir', type=Path, default=config_home / 'autostart',
                        help='로그인 자동 실행 항목 디렉터리')
    parser.add_argument('--no-autostart', action='store_true', help='로그인 USB 경고를 등록하지 않음')
    args = parser.parse_args(argv)
    for dependency in ('git', 'rsync', 'lsblk', 'findmnt'):
        if not shutil.which(dependency):
            raise updater.UpdateError(f'필요한 명령이 없습니다: {dependency}')
    source = Path(__file__).resolve().parent
    repository = args.repo
    if not repository:
        try:
            repository = updater.git(source, 'remote', 'get-url', 'origin')
        except updater.UpdateError as exc:
            raise updater.UpdateError('원격 저장소가 없습니다. GitHub 게시 후 --repo URL을 지정하세요.') from exc
    repository = updater.validate_repository(repository)
    root = args.data_dir.expanduser().absolute()
    bin_dir = args.bin_dir.expanduser().absolute()
    launcher = bin_dir / '42usb'
    autostart = None if args.no_autostart else args.autostart_dir.expanduser().absolute() / '42_usb.desktop'
    desktop_content = desktop_entry(launcher) if autostart else None
    if autostart and not any(shutil.which(name) for name in ('zenity', 'notify-send')):
        raise updater.UpdateError('로그인 경고에는 zenity 또는 notify-send가 필요합니다. 등록을 제외하려면 --no-autostart를 사용하세요.')
    if os.path.lexists(launcher):
        raise updater.UpdateError(f'기존 실행 파일을 덮어쓰지 않습니다: {launcher}')
    if os.path.lexists(root):
        raise updater.UpdateError(f'기존 설치 디렉터리를 덮어쓰지 않습니다: {root}')
    if autostart and os.path.lexists(autostart):
        raise updater.UpdateError(f'기존 자동 실행 항목을 덮어쓰지 않습니다: {autostart}')
    for path in [root, bin_dir] + ([autostart.parent] if autostart else []):
        for parent in (path, *path.parents):
            if parent.is_symlink():
                raise updater.UpdateError(f'설치 경로의 심볼릭 링크는 지원하지 않습니다: {parent}')
    branch = updater.validate_branch(args.branch) if args.branch else updater.default_branch(repository)
    print(f'설치 저장소: {repository}\n배포 브랜치: {branch}\n설치 위치: {root}', flush=True)
    commit = updater.initialize(root, repository, branch)
    published = []
    try:
        bin_dir.mkdir(parents=True, exist_ok=True)
        release, _ = updater.current_release(root)
        template = (release / 'launcher.py').read_text()
        marker = 'INSTALL_ROOT = None'
        if template.count(marker) != 1:
            raise updater.UpdateError('런처 템플릿 형식이 올바르지 않습니다.')
        template = template.replace(marker, 'INSTALL_ROOT = ' + repr(str(root)), 1)
        published.append(publish_new(launcher, template, 0o755))
        if autostart:
            published.append(publish_new(autostart, desktop_content, 0o644))
        config = updater.load_config(root)
        config['autostart'] = str(autostart) if autostart else None
        updater.atomic_json(root / updater.CONFIG_NAME, config)
    except BaseException:
        for path, device, inode in reversed(published):
            if os.path.lexists(path):
                current = path.lstat()
                if (current.st_dev, current.st_ino) == (device, inode):
                    path.unlink()
        if not os.path.lexists(launcher):
            shutil.rmtree(root)
        raise
    print(f'설치 완료: {launcher} ({commit[:8]})')
    if autostart:
        print(f'로그인 USB 경고 등록: {autostart}')
    if str(bin_dir) not in os.environ.get('PATH', '').split(os.pathsep):
        print('설치 디렉터리를 PATH에 추가하세요. 기본 설치: export PATH="$HOME/.local/bin:$PATH"')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print('\n설치를 취소했습니다.', file=sys.stderr)
        sys.exit(130)
    except (updater.UpdateError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f'42usb 설치: {exc}', file=sys.stderr)
        sys.exit(1)
