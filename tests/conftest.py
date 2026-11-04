"""Keep tests away from the operator's state, tmux servers and IPC."""
from __future__ import annotations

import os
from pathlib import Path
import pwd
import shutil
import subprocess
import sys
import tempfile
import threading

import pytest


_REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
_active = False
_private_roots: set[Path] = set()
_original_popen = subprocess.Popen
_original_open = os.open
_open_context = threading.local()


def _path(value, *, dir_fd=None, follow_leaf=True) -> Path | None:
    if isinstance(value, (str, bytes, os.PathLike)):
        path = Path(os.fsdecode(value))
        if not path.is_absolute() and dir_fd not in (None, -1, -100):
            path = Path(os.readlink(f"/proc/self/fd/{dir_fd}")) / path
        if follow_leaf:
            return path.resolve()
        # Entry operations follow parent directories, but never their leaf.
        return Path(os.path.abspath(path.parent.resolve() / path.name))
    return None


def _private(value) -> bool:
    path = _path(value)
    return _private_path(path)


def _private_path(path) -> bool:
    return path is not None and any(path.is_relative_to(root) for root in _private_roots)


def _guard(event, args):
    if not _active:
        return
    if event == "tempfile.mkdtemp":
        # Some terminal fixtures need a short /tmp path for AF_UNIX's limit.
        path = _path(args[0])
        if path and not path.is_relative_to(_REAL_HOME):
            _private_roots.add(path)
    elif event == "open":
        path, mode, flags = args
        writing = (flags or 0) & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
        target = _path(path, dir_fd=getattr(_open_context, "dir_fd", None))
        if writing and target and target.is_relative_to(_REAL_HOME) and not _private(target):
            raise PermissionError(f"test attempted to write operator file: {target}")
    elif event in {"os.mkdir", "os.remove", "os.rmdir", "os.rename", "os.chmod", "os.truncate"}:
        if event == "os.rename":
            entries = [(args[0], args[2]), (args[1], args[3])]
        else:
            fd_index = {"os.remove": 1, "os.rmdir": 1, "os.mkdir": 2, "os.chmod": 2}.get(event)
            entries = [(args[0], args[fd_index] if fd_index is not None else None)]
        for value, directory_fd in entries:
            target = _path(value, dir_fd=directory_fd, follow_leaf=event in {"os.chmod", "os.truncate"})
            if target and target.is_relative_to(_REAL_HOME) and not _private_path(target):
                raise PermissionError(f"test attempted to mutate operator path: {target}")
    elif event == "socket.connect":
        address = args[1]
        if isinstance(address, (str, bytes)) and not _private(address):
            raise PermissionError("test attempted non-private Unix IPC")
    elif event == "subprocess.Popen":
        executable, argv, cwd, env = args
        name = Path(os.fsdecode(executable)).name
        argv = [os.fsdecode(arg) for arg in argv]
        if name == "tmux":
            targets = []
            for index, arg in enumerate(argv[1:], 1):
                if arg[:2] not in ("-S", "-L"):
                    continue
                value = arg[2:] or (argv[index + 1] if index + 1 < len(argv) else "")
                targets.append(_private(value) if arg[:2] == "-S" else
                               bool(value and "/" not in value and
                                    _private((env or os.environ).get("TMUX_TMPDIR"))))
            # The setup fixture's private wrapper adds its explicit -S.
            allowed = all(targets) if targets else _private(executable)
            if not allowed:
                raise PermissionError("test tmux subprocess requires a private socket")
        if name == "systemctl" and not _private(executable):
            raise PermissionError("tests cannot control operator services")
        protected = str(_REAL_HOME / ".claude/local/tmux")
        if any(protected in arg for arg in argv):
            raise PermissionError("test subprocess references operator tmux state/IPC")


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    global _active
    root = Path(tempfile.mkdtemp(prefix="ct-tests-", dir="/tmp"))
    _private_roots.add(root)
    saved_environment = dict(os.environ)
    saved_tempdir, saved_bytecode = tempfile.tempdir, sys.dont_write_bytecode
    config._tmux_isolation = root, saved_environment, saved_tempdir, saved_bytecode
    for name in ("TMUX", "TMUX_PANE", "CLAUDE_TMUX_THEME", "DBUS_SESSION_BUS_ADDRESS",
                 "CLAUDE_CONFIG_DIR", "PI_CODING_AGENT_DIR", "OMP_CODING_AGENT_DIR"):
        os.environ.pop(name, None)
    paths = {"HOME": root / "home", "TMPDIR": root / "tmp", "TMUX_TMPDIR": root / "tmux",
             "CODEX_HOME": root / "home/.codex", "XDG_CONFIG_HOME": root / "home/.config",
             "XDG_DATA_HOME": root / "home/.local/share", "XDG_CACHE_HOME": root / "cache",
             "XDG_RUNTIME_DIR": root / "runtime", "HYPOTHESIS_STORAGE_DIRECTORY": root / "hypothesis"}
    for name, path in paths.items():
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.environ[name] = str(path)
    os.environ["CLAUDE_TMUX_SOCKET"] = str(root / "tmux/unavailable.sock")
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    tempfile.tempdir = str(paths["TMPDIR"])
    sys.dont_write_bytecode = True
    config.option.basetemp = str(root / "pytest")
    # cacheprovider may configure before this hook. Redirect both its existing
    # object and subsequent getini calls, rather than changing parsed options.
    cache_dir = root / "pytest-cache"
    config._inicache["cache_dir"] = str(cache_dir)
    if hasattr(config, "cache"):
        config.cache._cachedir = cache_dir

    class PrivatePopen(_original_popen):
        def __init__(self, *args, **kwargs):
            environment = dict(kwargs.get("env") if kwargs.get("env") is not None else os.environ)
            for name in ("HOME", "TMPDIR", "TMUX_TMPDIR", "XDG_RUNTIME_DIR"):
                environment.setdefault(name, os.environ[name])
                if not _private(environment[name]):
                    raise PermissionError(f"test subprocess has non-private {name}")
            if environment.get("TMUX"):
                server = environment["TMUX"].rsplit(",", 2)[0]
                # Negative transport tests need malformed/nonabsolute input.
                # Absolute sockets must still belong to the disposable roots.
                if Path(server).is_absolute() and not _private(server):
                    raise PermissionError("test subprocess inherited an operator tmux server")
            for name in ("CODEX_HOME", "PI_CODING_AGENT_DIR", "OMP_CODING_AGENT_DIR", "CLAUDE_CONFIG_DIR"):
                if environment.get(name) and not _private(environment[name]):
                    raise PermissionError(f"test subprocess has non-private {name}")
            kwargs["env"] = environment
            super().__init__(*args, **kwargs)

    subprocess.Popen = PrivatePopen
    def private_open(path, flags, mode=0o777, *, dir_fd=None):
        # CPython's open audit event omits dir_fd. Carry it only around the
        # actual os.open call, so its audit check resolves the correct target.
        previous = getattr(_open_context, "dir_fd", None)
        _open_context.dir_fd = dir_fd
        try:
            return _original_open(path, flags, mode, dir_fd=dir_fd)
        finally:
            _open_context.dir_fd = previous

    os.open = private_open
    if _original_open in os.supports_dir_fd:
        os.supports_dir_fd.add(private_open)
    sys.addaudithook(_guard)
    _active = True


def pytest_unconfigure(config):
    global _active
    isolation = getattr(config, "_tmux_isolation", None)
    if isolation is None:
        return
    _active = False
    root, environment, saved_tempdir, saved_bytecode = isolation
    subprocess.Popen = _original_popen
    os.supports_dir_fd.discard(os.open)
    os.open = _original_open
    tempfile.tempdir, sys.dont_write_bytecode = saved_tempdir, saved_bytecode
    os.environ.clear()
    os.environ.update(environment)
    shutil.rmtree(root)
