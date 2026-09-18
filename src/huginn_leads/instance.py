"""An OS-owned lock shared by application startup and offline restore."""
from pathlib import Path
import os


class InstanceLock:
    def __init__(self, database: Path):
        self.path = database.with_suffix('.lock')
        self.file = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open('a+b')
        try:
            if handle.seek(0, 2) == 0:
                handle.write(b'0')
                handle.flush()
            handle.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise RuntimeError('application_running: 请先停止此目录的 Huginn Leads，再进行启动或恢复。') from None
        self.file = handle
        return self

    def release(self):
        if self.file is not None:
            self.file.close()  # OS also releases the lock if the process crashes.
            self.file = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *_args):
        self.release()
