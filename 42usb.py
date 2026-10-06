#!/usr/bin/env python3
"""Move home data to an ext4 USB volume and restore it (Linux)."""
import argparse
import ctypes
import fcntl
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import uuid


class Error(Exception):
    pass


def run(args):
    result = subprocess.run(args, text=True, capture_output=True)
    if result.returncode:
        raise Error(f"{args[0]} 실패: {result.stderr.strip()}")
    return result.stdout


def exists(path):
    return os.path.lexists(path)


def beneath(path, parent):
    return path == parent or parent in path.parents


def plain_parents(path, root):
    if not beneath(path, root):
        raise Error(f"허용 범위 밖의 경로입니다: {path}")
    current = root
    if root.is_symlink():
        raise Error(f"기준 디렉터리가 심볼릭 링크입니다: {root}")
    for part in path.relative_to(root).parts:
        current /= part
        if current.is_symlink():
            raise Error(f"상위 경로의 심볼릭 링크는 지원하지 않습니다: {current}")
        if exists(current) and not current.is_dir():
            raise Error(f"디렉터리가 아닌 경로입니다: {current}")


def home_path(value, home):
    path = Path(os.path.abspath(os.path.expanduser(value)))
    if path == home or not beneath(path, home):
        raise Error(f"홈 디렉터리 내부 경로를 지정하세요. 예: {home}/.local/share/nvim")
    plain_parents(path.parent, home)
    return path


def usb_devices():
    data = json.loads(run(['lsblk', '--tree', '--json', '--bytes', '--output',
                          'PATH,TYPE,TRAN,SIZE,FSTYPE,LABEL,UUID,MOUNTPOINTS,MODEL']))
    disks, volumes = [], []

    def walk(node, disk=None):
        if node.get('tran') == 'usb' and disk is None:
            disk = node
            disks.append(node)
        if disk is not None and node.get('fstype'):
            volumes.append(dict(node, disk=disk['path'], model=disk.get('model')))
        for child in node.get('children', []):
            walk(child, disk)

    for node in data['blockdevices']:
        walk(node)
    return disks, volumes


def describe(disks, volumes):
    print(f"연결된 USB 장치: {len(disks)}개 / 파일시스템: {len(volumes)}개")
    for disk in disks:
        print(f"  장치 {disk['path']} | {disk.get('model') or '?'} | "
              f"{disk.get('size', 0) / 1024**3:.1f} GiB")
    for index, volume in enumerate(volumes, 1):
        mounts = ', '.join(m for m in volume.get('mountpoints', []) if m) or '마운트되지 않음'
        print(f"  [{index}] {volume['path']} | {volume['fstype']} | "
              f"{volume.get('label') or '(레이블 없음)'} | {mounts}")


def choose(volumes):
    if not volumes:
        raise Error('사용 가능한 USB 파일시스템이 없습니다.')
    if len(volumes) == 1:
        chosen = volumes[0]
    else:
        try:
            index = int(input('사용할 USB 파일시스템 번호: ')) - 1
            if index < 0:
                raise ValueError
            chosen = volumes[index]
        except (ValueError, IndexError):
            raise Error('올바른 번호를 입력하세요.')
    if chosen['fstype'] != 'ext4':
        raise Error('ext4 USB만 지원합니다. 포맷은 자동으로 변경하지 않습니다.')
    mounts = [Path(m) for m in chosen.get('mountpoints', []) if m]
    if len(mounts) != 1:
        raise Error('USB를 한 위치에 마운트한 다음 다시 실행하세요.')
    if not chosen.get('uuid'):
        raise Error('USB 파일시스템 UUID를 확인할 수 없습니다.')
    return chosen, mounts[0]


def human_size(size):
    value = float(size)
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if value < 1024 or unit == 'TiB':
            return f'{value:.1f} {unit}'
        value /= 1024


def tree_usage(root):
    """Logical bytes and allocated bytes; never traverse links or mounts."""
    if not exists(root):
        return 0, 0
    device = root.lstat().st_dev
    logical = allocated = 0
    seen = set()
    nodes = [root]
    while nodes:
        path = nodes.pop()
        s = path.lstat()
        if s.st_dev != device:
            continue
        identity = (s.st_dev, s.st_ino)
        if identity in seen:
            continue
        seen.add(identity)
        allocated += s.st_blocks * 512
        if stat.S_ISREG(s.st_mode) or stat.S_ISLNK(s.st_mode):
            logical += s.st_size
        elif stat.S_ISDIR(s.st_mode):
            if path != root and os.path.ismount(path):
                continue
            nodes.extend(path.iterdir())
    return logical, allocated


def mount_info(volume, mount):
    info = json.loads(run(['findmnt', '--json', '--mountpoint', str(mount),
                           '--output', 'TARGET,FSTYPE,UUID,OPTIONS']))['filesystems'][0]
    if info['fstype'] != volume['fstype'] or info['uuid'] != volume.get('uuid'):
        raise Error('마운트된 USB의 파일시스템 또는 UUID가 일치하지 않습니다.')
    return info


def check_volume(volume, mount):
    # Check both the physical USB inventory and the actual mount before writes.
    _, current = usb_devices()
    if not any(v['path'] == volume['path'] and v.get('uuid') == volume['uuid']
               and v.get('fstype') == 'ext4'
               and str(mount) in (v.get('mountpoints') or []) for v in current):
        raise Error('선택한 USB가 분리되었거나 마운트 정보가 바뀌었습니다.')
    info = mount_info(volume, mount)
    if 'rw' not in info['options'].split(',') or not os.access(mount, os.W_OK):
        raise Error('USB에 쓰기 권한이 없습니다.')


def inspect_tree(source):
    """Reject special files, nested mounts and relocation-sensitive links."""
    root = source if source.is_dir() else source.parent
    device = source.lstat().st_dev
    size = 0
    nodes = [source]
    while nodes:
        path = nodes.pop()
        s = path.lstat()
        if s.st_dev != device or (path != source and os.path.ismount(path)):
            raise Error(f'하위 마운트는 이동할 수 없습니다: {path}')
        if stat.S_ISLNK(s.st_mode):
            target = os.readlink(path)
            normalized = Path(os.path.abspath(path.parent / target))
            if not os.path.isabs(target) and not beneath(normalized, root):
                raise Error(f'이동 시 의미가 바뀌는 외부 상대 심볼릭 링크입니다: {path}')
        elif stat.S_ISDIR(s.st_mode):
            nodes.extend(path.iterdir())
        elif stat.S_ISREG(s.st_mode):
            size += s.st_size
        else:
            raise Error(f'소켓·파이프·장치 파일은 이동할 수 없습니다: {path}')
    return size


def check_in_use(source):
    """Best-effort /proc check; the caller must still close all writers."""
    for process in Path('/proc').iterdir():
        if not process.name.isdigit():
            continue
        try:
            if process.stat().st_uid != os.getuid():
                continue
            paths = [process / 'cwd', process / 'exe'] + list((process / 'fd').iterdir())
            for entry in paths:
                try:
                    target = Path(os.readlink(entry))
                except OSError:
                    continue
                if target.is_absolute() and beneath(target, source):
                    raise Error(f'대상 데이터를 사용하는 프로세스를 종료하세요: PID {process.name} ({target})')
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def rename_new(source, destination):
    # Linux atomic rename with NOREPLACE, including dangling symlinks.
    libc = ctypes.CDLL(None, use_errno=True)
    result = libc.renameat2(-100, os.fsencode(source), -100, os.fsencode(destination), 1)
    if result:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), str(destination))
    sync_dir(destination.parent)
    if source.parent != destination.parent:
        sync_dir(source.parent)


def remove(path):
    if path.is_symlink() or not path.is_dir():
        path.unlink()
    else:
        shutil.rmtree(path)


def copy(source, container):
    run(['rsync', '-aHAX', '--no-owner', '--no-group', '--fsync', '--',
         str(source), str(container) + '/'])
    sync_dir(container)


def verify(source, destination):
    suffix = '/' if source.is_dir() else ''
    changes = run(['rsync', '-aHAXnc', '--no-owner', '--no-group', '--delete',
                   '--itemize-changes', '--out-format=%i %n', '--',
                   str(source) + suffix, str(destination) + suffix])
    if changes.strip():
        raise Error('복사본 검증 실패 또는 작업 중 파일 변경이 감지되었습니다.\n' + changes[:1500])


def save_journal(path, data):
    temporary = path.with_suffix('.tmp')
    with temporary.open('x') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temporary, path)
    sync_dir(path.parent)


def read_registry(state):
    path = state / 'registry'
    if not exists(path):
        return {}
    if path.is_symlink():
        raise Error('USB 기록 파일이 심볼릭 링크입니다.')
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise Error('USB 기록 파일 형식이 올바르지 않습니다.')
    return data


def record_link(state, local, entry):
    registry = read_registry(state)
    if entry is None:
        registry.pop(str(local), None)
    else:
        registry[str(local)] = entry
    save_journal(state / 'registry', registry)


def home_links(home):
    links, errors = [], []

    def onerror(exc):
        errors.append(str(exc))

    for base, directories, files in os.walk(home, followlinks=False, onerror=onerror):
        directories[:] = [name for name in directories
                          if not os.path.ismount(Path(base) / name)]
        for name in directories + files:
            path = Path(base) / name
            if not path.is_symlink():
                continue
            try:
                target = Path(os.path.abspath(path.parent / os.readlink(path)))
                if '42_home' in target.parts:
                    links.append((path, target))
            except OSError as exc:
                errors.append(str(exc))
    return sorted(links), errors


def link_status(local, target, registry, volumes, mounted):
    entry = registry.get(str(local))
    candidates = [(v, Path(m)) for v in volumes for m in v.get('mountpoints', [])
                  if m and beneath(target, Path(m))]
    if entry:
        if str(target) != entry['remote']:
            return '주의: 기록과 다른 대상의 심볼릭 링크'
        if not any(v.get('uuid') == entry['uuid'] for v in volumes):
            return 'USB 미연결: 기록된 UUID의 장치 없음'
        if not candidates:
            return '마운트 경로 불일치 또는 USB 미마운트'
        if not any(v.get('uuid') == entry['uuid'] for v, _ in candidates):
            return '주의: 같은 경로에 다른 UUID의 USB가 마운트됨'
        candidates = [(v, m) for v, m in candidates if v.get('uuid') == entry['uuid']]
    if candidates:
        volume, mount = max(candidates, key=lambda pair: len(pair[1].parts))
        if not mounted.get((volume['path'], str(mount))):
            return '주의: USB 마운트 검증 실패'
        if not local.exists():
            return '주의: USB 마운트 정상 / 링크 대상 없음'
        suffix = '' if entry else ' / UUID 사전 기록 없음'
        return '정상: USB 마운트 및 링크 대상 확인' + suffix
    if '42_home' in target.parts:
        return '주의: 해당 경로의 USB 마운트를 확인할 수 없음'
    return '정상: 일반 디렉터리 링크' if local.exists() else '끊긴 링크: 대상 없음'


def status(home, state, disks, volumes):
    describe(disks, volumes)
    mounted = {}
    for volume in volumes:
        mounts = [Path(m) for m in volume.get('mountpoints', []) if m]
        if not mounts:
            print(f"\n{volume['path']}: 미마운트 — 42_home 및 남은 용량 계산 불가")
        for mount in mounts:
            print(f"\nUSB 용량: {mount}", flush=True)
            try:
                info = mount_info(volume, mount)
                mounted[(volume['path'], str(mount))] = True
                usage = shutil.disk_usage(mount)
                mode = '읽기/쓰기' if 'rw' in info['options'].split(',') else '읽기 전용'
                print(f"  마운트: 정상 ({volume['fstype']}, {mode}) / UUID {volume.get('uuid')}")
                print(f'  전체: {human_size(usage.total)} / 남은 용량: {human_size(usage.free)}')
                root = mount / '42_home'
                if root.is_symlink():
                    print('  42_home: 심볼릭 링크이므로 용량 계산 제외')
                elif not root.exists():
                    print('  42_home: 없음 (0 B)')
                else:
                    print('  42_home 용량 계산 중…', flush=True)
                    logical, allocated = tree_usage(root)
                    print(f'  42_home: {human_size(allocated)} (실제 점유) / {human_size(logical)} (파일 크기 합계)')
            except (Error, OSError, ValueError, KeyError) as exc:
                print(f'  확인 실패: {exc}')
    plain_parents(state, home)
    registry = read_registry(state)
    print('\n42_home 대상 심볼릭 링크:', flush=True)
    links, errors = home_links(home)
    for local, target in links:
        print(f'  {local.relative_to(home)} → {target}')
        print(f'    {link_status(local, target, registry, volumes, mounted)}')
    if not links:
        print('  해당 링크가 없습니다.')
    linked = {str(local) for local, _ in links}
    for local in sorted(set(registry) - linked):
        if Path(local).is_symlink():
            continue
        print(f'  주의: 이동 기록이 있으나 심볼릭 링크를 찾지 못함: {local}')
    if errors:
        print(f'  읽기 권한 또는 경로 변경으로 확인하지 못한 항목: {len(errors)}개')
    records = sorted(list(state.glob('*.json')) + list(state.glob('*.tmp')))
    print(f'\n미완료 작업 기록: {len(records)}개')
    for path in records:
        print(f'{path}\n{path.read_text()}')


def transfer(home, local, remote, restore, state, guard=lambda: None, registry_entry=None):
    """Copy/verify, atomically publish, then remove the old copy.

    Every failure leaves at least one complete copy. After publication we never
    roll back automatically: the published copy may already have new writes.
    """
    source, destination = (remote, local) if restore else (local, remote)
    if source.is_symlink() or not exists(source):
        raise Error(f'원본이 없거나 심볼릭 링크입니다: {source}')
    if os.path.ismount(source):
        raise Error('마운트 지점 자체는 이동할 수 없습니다.')
    if restore:
        if not local.is_symlink() or Path(os.path.abspath(local.parent / os.readlink(local))) != remote:
            raise Error('홈 경로가 선택한 USB의 해당 경로를 가리키는 심볼릭 링크가 아닙니다.')
    elif exists(destination):
        raise Error(f'USB 대상이 이미 있습니다. 덮어쓰지 않습니다: {destination}')
    size = inspect_tree(source)
    check_in_use(source)
    ancestor = destination.parent
    while not ancestor.exists():
        ancestor = ancestor.parent
    if shutil.disk_usage(ancestor).free < size + 16 * 1024**2:
        raise Error(f'대상 저장 공간이 부족합니다. 최소 약 {size / 1024**2 + 16:.1f} MiB 필요')
    guard()
    destination.parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    container = destination.parent / ('.42usb-stage-' + token)
    backup = source.parent / ('.42usb-backup-' + token)
    link_backup = local.parent / ('.42usb-link-' + token)
    journal = state / (token + '.json')
    data = dict(operation='restore' if restore else 'move', local=str(local),
                remote=str(remote), source=str(source), destination=str(destination),
                stage=str(container), backup=str(backup), link_backup=str(link_backup),
                phase='copying')
    save_journal(journal, data)
    published = False
    try:
        container.mkdir(mode=0o700)
        copied = container / source.name
        print(f'복사 및 내용 검증 중: {size / 1024**2:.1f} MiB', flush=True)
        copy(source, container)
        verify(source, copied)
        check_in_use(source)
        guard()
        data['phase'] = 'switching'
        save_journal(journal, data)
        rename_new(source, backup)
        verify(backup, copied)
        guard()
        if restore:
            if not local.is_symlink() or Path(os.path.abspath(local.parent / os.readlink(local))) != remote:
                raise Error('작업 중 홈 심볼릭 링크가 변경되었습니다.')
            rename_new(local, link_backup)
            rename_new(copied, local)
            published = True
        else:
            rename_new(copied, remote)
            local.symlink_to(remote, target_is_directory=remote.is_dir())
            published = True
            sync_dir(local.parent)
        data['phase'] = 'published'
        save_journal(journal, data)
        if registry_entry is not None:
            record_link(state, local, None if restore else registry_entry)
        guard()
        # A writer holding an old descriptor can still modify the renamed source.
        check_in_use(backup)
        verify(backup, destination)
        remove(backup)
        if exists(link_backup):
            link_backup.unlink()
        container.rmdir()
        sync_dir(source.parent)
        sync_dir(destination.parent)
        journal.unlink()
        sync_dir(state)
    except BaseException:
        if not published:
            try:
                # Do not overwrite anything created by another application.
                if exists(backup):
                    rename_new(backup, source)
                if exists(link_backup):
                    rename_new(link_backup, local)
                if exists(container):
                    remove(container)
                # A published USB copy is deliberately retained on link failure.
                journal.unlink()
                sync_dir(state)
            except BaseException:
                print(f'자동 원상복구가 끝나지 않았습니다. 기록: {journal}', file=sys.stderr)
        else:
            print(f'전환은 완료됐지만 원본 정리가 끝나지 않았습니다. 기록: {journal}', file=sys.stderr)
        raise
    print(f"완료: {local}" + ('' if restore else f' → {remote}'))


def main(argv=None):
    parser = argparse.ArgumentParser(description='홈 데이터를 ext4 USB의 42_home으로 이동/복원합니다.',
                                     epilog='예: 42usb ~/.local/share/nvim | 42usb restore ~/.local/share/nvim')
    parser.add_argument('action', help='홈 내부 경로 또는 restore / list / status')
    parser.add_argument('path', nargs='?')
    args = parser.parse_args(argv)
    home = Path.home().absolute()
    state = home / '.42usb-state'
    disks, volumes = usb_devices()
    if args.action == 'status':
        if args.path:
            parser.error('status에는 경로를 지정하지 않습니다.')
        status(home, state, disks, volumes)
        return 0
    describe(disks, volumes)
    if args.action == 'list':
        if args.path:
            parser.error('list에는 경로를 지정하지 않습니다.')
        return 0
    restore = args.action == 'restore'
    value = args.path if restore else args.action
    if not value or (not restore and args.path):
        parser.error('사용법: 42usb PATH 또는 42usb restore PATH')
    for command in ('rsync', 'findmnt'):
        if not shutil.which(command):
            raise Error(f'필요한 명령이 없습니다: {command}')
    local = home_path(value, home)
    for protected in [state, Path(__file__).resolve(), home / '.local/bin/42usb']:
        if beneath(protected, local) or beneath(local, state):
            raise Error(f'42usb 자체 또는 작업 기록을 포함하는 경로는 이동할 수 없습니다: {local}')
    if not sys.stdin.isatty():
        raise Error('USB 확인을 위해 대화형 터미널에서 실행하세요.')
    volume, mount = choose(volumes)
    check_volume(volume, mount)
    remote = mount / '42_home' / local.relative_to(home)
    plain_parents(remote.parent, mount)
    plain_parents(state, home)
    registered = read_registry(state).get(str(local))
    if restore and registered and registered.get('uuid') != volume['uuid']:
        raise Error('이 경로를 이동했던 USB의 UUID와 다릅니다. 원래 USB를 선택하세요.')
    source, destination = (remote, local) if restore else (local, remote)
    if source.is_symlink() or not exists(source):
        raise Error(f'원본이 없거나 심볼릭 링크입니다: {source}')
    print('대상 데이터 용량 계산 중…', flush=True)
    logical, allocated = tree_usage(source)
    ancestor = destination.parent
    while not ancestor.exists():
        ancestor = ancestor.parent
    available = shutil.disk_usage(ancestor).free
    print(f"\n{'복원' if restore else '이동'}: {remote if restore else local}")
    print(f"  대상: {local if restore else remote}")
    print(f'  데이터 용량: {human_size(allocated)} (실제 점유) / {human_size(logical)} (파일 크기 합계)')
    print(f'  도착지 남은 용량: {human_size(available)}')
    print(f"  USB: {volume['path']} / UUID {volume['uuid']}")
    print('대상 데이터를 사용하는 프로그램을 모두 종료해야 합니다.')
    if input('이 USB가 맞고 프로그램을 종료했다면 yes 입력: ').strip() != 'yes':
        print('취소했습니다.')
        return 0
    plain_parents(state, home)
    state.mkdir(mode=0o700, exist_ok=True)
    lock_fd = os.open(state / 'lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, 'w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Error('다른 42usb 작업이 진행 중입니다.')
        if list(state.glob('*.json')) or list(state.glob('*.tmp')):
            raise Error('미완료 작업이 있습니다. 42usb status로 기록을 확인하고 원본·복사본을 점검하세요.')
        check_volume(volume, mount)
        plain_parents(remote.parent, mount)
        plain_parents(local.parent, home)
        entry = dict(uuid=volume['uuid'], mount=str(mount), remote=str(remote),
                     device=volume['path'], label=volume.get('label'))
        transfer(home, local, remote, restore, state,
                 lambda: check_volume(volume, mount), registry_entry=entry)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (Error, OSError, EOFError, ValueError) as exc:
        print(f'42usb: {exc}', file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print('\n42usb: 중단했습니다. 미완료 기록은 42usb status로 확인하세요.', file=sys.stderr)
        sys.exit(130)
