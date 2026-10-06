"""Git-backed, immutable releases for 42usb. No third-party packages."""
import ast
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import uuid

CHECK_TIMEOUT = 5
LOCAL_TIMEOUT = 15
INSTALL_TIMEOUT = 60
CONFIG_NAME = 'install.json'


class UpdateError(Exception):
    pass


def clean_text(text):
    return ''.join(c if c.isprintable() else ' ' for c in str(text)).strip()


def command(args, timeout=LOCAL_TIMEOUT):
    env = dict(os.environ, GIT_TERMINAL_PROMPT='0', GIT_PAGER='cat',
               GIT_SSH_COMMAND='ssh -o BatchMode=yes -o ConnectTimeout=5',
               PYTHONDONTWRITEBYTECODE='1', LC_ALL='C')
    for name in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_COMMON_DIR',
                 'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES'):
        env.pop(name, None)
    process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, encoding='utf-8', errors='replace',
                               env=env, start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except BaseException:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate()
        raise
    if process.returncode:
        detail = clean_text(stderr)[-1000:]
        raise UpdateError(f'{Path(args[0]).name} 실패: {detail}')
    return stdout.strip()


def git(directory, *args, timeout=LOCAL_TIMEOUT):
    return command(['git', '-c', 'core.hooksPath=/dev/null',
                    '-c', 'protocol.ext.allow=never', '-c', 'submodule.recurse=false',
                    '-C', str(directory), *args], timeout=timeout)


def validate_repository(value):
    """Local repositories are supported for offline development and tests."""
    if re.fullmatch(r'https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/?', value):
        return value.rstrip('/')
    if re.fullmatch(r'git@github\.com:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', value):
        return value
    local = Path(value).expanduser()
    if local.is_absolute() and local.is_dir():
        git(local, 'rev-parse', '--git-dir')
        return str(local.resolve())
    raise UpdateError('GitHub HTTPS/SSH 주소 또는 절대 경로의 로컬 Git 저장소를 지정하세요.')


def validate_branch(branch):
    if not branch or branch.startswith('-') or branch == 'HEAD':
        raise UpdateError('올바른 배포 브랜치를 지정하세요.')
    command(['git', 'check-ref-format', 'refs/heads/' + branch])
    return branch


def default_branch(repository):
    output = command(['git', 'ls-remote', '--symref', repository, 'HEAD'], timeout=INSTALL_TIMEOUT)
    for line in output.splitlines():
        match = re.fullmatch(r'ref: refs/heads/(.+)\s+HEAD', line)
        if match:
            return validate_branch(match.group(1))
    raise UpdateError('기본 브랜치를 찾지 못했습니다. --branch로 지정하세요.')


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_json(path, value):
    temporary = path.parent / ('.config-' + uuid.uuid4().hex)
    try:
        with temporary.open('x') as stream:
            os.chmod(temporary, 0o600)
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def load_config(root):
    path = root / CONFIG_NAME
    if path.is_symlink():
        raise UpdateError('설치 설정 파일은 심볼릭 링크일 수 없습니다.')
    config = json.loads(path.read_text())
    if config.get('schema') != 1:
        raise UpdateError('지원하지 않는 설치 설정 버전입니다.')
    validate_branch(config['branch'])
    repository = config['repository']
    if not isinstance(repository, str) or not repository:
        raise UpdateError('업데이트 저장소 주소가 없습니다.')
    return config


def current_release(root):
    app = root / 'app'
    if not app.is_symlink():
        raise UpdateError('설치본 app 링크가 없거나 변경되었습니다.')
    release = app.resolve(strict=True)
    if release.parent != (root / 'releases').resolve():
        raise UpdateError('설치본이 releases 디렉터리 밖을 가리킵니다.')
    commit = git(release, 'rev-parse', 'HEAD')
    if not re.fullmatch(r'[0-9a-f]{40,64}', commit) or release.name != commit:
        raise UpdateError('설치본 커밋과 릴리스 경로가 일치하지 않습니다.')
    return release, commit


def ensure_clean(release):
    if git(release, 'status', '--porcelain', '--untracked-files=all', '--ignored'):
        raise UpdateError('설치본에 직접 수정하거나 추가한 파일이 있습니다. 기존 파일을 보존하며 업데이트를 중단합니다.')


def fetch_candidate(root, config, timeout=CHECK_TIMEOUT):
    repository = root / 'repository.git'
    if git(repository, 'remote', 'get-url', 'origin') != config['repository']:
        raise UpdateError('설치 시 지정한 저장소와 origin 주소가 다릅니다.')
    branch = config['branch']
    reference = f'refs/remotes/origin/{branch}'
    git(repository, 'fetch', '--no-tags', '--no-recurse-submodules', 'origin',
        f'+refs/heads/{branch}:{reference}', timeout=timeout)
    return git(repository, 'rev-parse', reference + '^{commit}')


def is_ancestor(repository, ancestor, descendant):
    return git(repository, 'merge-base', ancestor, descendant) == ancestor


def validate_release(release):
    for name in ('42usb.py', 'updater.py', 'launcher.py', 'login_check.py'):
        path = release / name
        if path.is_symlink() or not path.is_file():
            raise UpdateError(f'릴리스 필수 파일이 없거나 심볼릭 링크입니다: {name}')
    for name in git(release, 'ls-files', '-z').split('\0'):
        if name.endswith('.py'):
            path = release / name
            if path.is_symlink():
                raise UpdateError(f'Python 파일의 심볼릭 링크는 허용하지 않습니다: {name}')
            try:
                ast.parse(path.read_text(encoding='utf-8'), filename=name)
            except (SyntaxError, UnicodeError) as exc:
                raise UpdateError(f'새 버전 문법 검사 실패: {name}: {exc}') from exc
    module = ast.parse((release / 'updater.py').read_text())
    if not any(isinstance(node, ast.FunctionDef) and node.name == 'run_cli' for node in module.body):
        raise UpdateError('새 버전에 호환되는 updater.run_cli 진입점이 없습니다.')
    command([sys.executable, '-I', '-B', str(release / '42usb.py'), '--help'])
    command([sys.executable, '-I', '-B', str(release / 'login_check.py'), '--help'])


def prepare_release(root, commit):
    if not re.fullmatch(r'[0-9a-f]{40,64}', commit):
        raise UpdateError('올바르지 않은 커밋 식별자입니다.')
    releases = root / 'releases'
    destination = releases / commit
    if os.path.lexists(destination):
        if destination.is_symlink() or git(destination, 'rev-parse', 'HEAD') != commit:
            raise UpdateError('기존 릴리스 경로를 덮어쓰지 않습니다.')
        ensure_clean(destination)
        validate_release(destination)
        return destination
    staging = Path(tempfile.mkdtemp(prefix='.prepare-', dir=releases))
    try:
        git(root / 'repository.git', 'update-ref', f'refs/42usb/releases/{commit}', commit)
        command(['git', '-c', 'core.hooksPath=/dev/null', 'clone', '--shared',
                 '--no-checkout', '--', str(root / 'repository.git'), str(staging)])
        git(staging, 'checkout', '--detach', commit)
        validate_release(staging)
        for base, directories, files in os.walk(staging):
            for name in files:
                path = Path(base) / name
                if not path.is_symlink():
                    with path.open('rb') as stream:
                        os.fsync(stream.fileno())
            sync_directory(Path(base))
        os.rename(staging, destination)
        sync_directory(releases)
        return destination
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def activate_release(root, release):
    temporary = root / ('.app-' + uuid.uuid4().hex)
    if os.path.lexists(root / 'app') and not (root / 'app').is_symlink():
        raise UpdateError('기존 app 경로를 덮어쓰지 않습니다.')
    try:
        temporary.symlink_to(Path('releases') / release.name, target_is_directory=True)
        os.replace(temporary, root / 'app')
        sync_directory(root)
    finally:
        if temporary.is_symlink():
            temporary.unlink()


def initialize(root, repository, branch):
    if os.path.lexists(root):
        raise UpdateError(f'기존 설치 디렉터리를 덮어쓰지 않습니다: {root}')
    root.mkdir(parents=True, mode=0o700)
    try:
        (root / 'releases').mkdir()
        command(['git', 'init', '--bare', str(root / 'repository.git')])
        git(root / 'repository.git', 'remote', 'add', 'origin', repository)
        config = dict(schema=1, repository=repository, branch=branch)
        atomic_json(root / CONFIG_NAME, config)
        commit = fetch_candidate(root, config, timeout=INSTALL_TIMEOUT)
        activate_release(root, prepare_release(root, commit))
        return commit
    except BaseException:
        shutil.rmtree(root)
        raise


def check_update(root, config, ask=None):
    if ask is None:
        ask = input
    release, installed = current_release(root)
    ensure_clean(release)
    candidate = fetch_candidate(root, config)
    if candidate == installed:
        return False
    repository = root / 'repository.git'
    if not is_ancestor(repository, installed, candidate):
        raise UpdateError('배포 브랜치의 이력이 설치본과 갈라졌거나 뒤로 이동했습니다. 자동 업데이트하지 않습니다.')
    count = git(repository, 'rev-list', '--count', f'{installed}..{candidate}')
    log = git(repository, 'log', '--max-count=5', '--format=%h %s', f'{installed}..{candidate}')
    print(f'\n새 커밋 {count}개가 있습니다.')
    for line in log.splitlines():
        print('  ' + clean_text(line))
    if int(count) > 5:
        print(f'  … 나머지 {int(count) - 5}개')
    if not sys.stdin.isatty():
        raise UpdateError('업데이트 확인은 완료했습니다. 적용하려면 대화형 터미널에서 42usb update를 실행하세요.')
    if ask('새 버전이 있습니다. 업데이트하시겠습니까? [y/N] ').strip().lower() not in ('y', 'yes'):
        print('현재 버전을 유지합니다.')
        return False
    print('새 버전을 준비하고 검사합니다…', flush=True)
    new_release = prepare_release(root, candidate)
    ensure_clean(release)
    activate_release(root, new_release)
    print(f'업데이트 완료: {installed[:8]} → {candidate[:8]}\n', flush=True)
    return True


@contextlib.contextmanager
def transfer_lock():
    """Interoperate with the existing data-movement lock."""
    state = Path.home() / '.42usb-state'
    if state.is_symlink():
        raise UpdateError('이동 기록 디렉터리가 심볼릭 링크입니다.')
    state.mkdir(mode=0o700, exist_ok=True)
    descriptor = os.open(state / 'lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise UpdateError('데이터 이동·복원이 진행 중이므로 업데이트하지 않습니다.') from exc
        if list(state.glob('*.json')) or list(state.glob('*.tmp')):
            raise UpdateError('미완료 데이터 이동 기록이 있습니다. 업데이트 전에 42usb status로 확인하세요.')
        yield
    finally:
        os.close(descriptor)


def run_cli(root, arguments):
    """Stable launcher API; launcher holds its run lock through this call."""
    args = list(arguments)
    skip = '--no-update-check' in args
    args = [arg for arg in args if arg != '--no-update-check']
    manual = bool(args and args[0] == 'update')
    if manual and (len(args) != 1 or skip):
        print('사용법: 42usb update (--no-update-check와 함께 사용할 수 없습니다.)', file=sys.stderr)
        return 2
    config = load_config(root)
    if args == ['--version']:
        _, commit = current_release(root)
        print(f"42usb {commit[:8]} ({config['branch']})")
        return 0
    help_only = not args or '--help' in args or '-h' in args
    should_check = manual or (not skip and not help_only and sys.stdin.isatty())
    if should_check:
        try:
            with transfer_lock():
                changed = check_update(root, config)
            if manual and not changed:
                print('업데이트 없이 현재 버전을 유지합니다.')
        except (UpdateError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
            detail = '네트워크 또는 검사 제한 시간 초과' if isinstance(exc, subprocess.TimeoutExpired) else clean_text(exc)
            print(f'42usb 업데이트: {detail}', file=sys.stderr)
            if manual:
                return 1
            print('기존 버전으로 명령을 계속 실행합니다.', file=sys.stderr)
        if manual:
            return 0
    release, _ = current_release(root)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1',
               FORTYTWO_USB_INSTALL_ROOT=str(root))
    result = subprocess.run([sys.executable, '-B', str(release / '42usb.py'), *(args or ['--help'])], env=env)
    if help_only:
        print('\n설치본 명령: 42usb update | 42usb --version | 42usb --no-update-check COMMAND')
    return result.returncode
