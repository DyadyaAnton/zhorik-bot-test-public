"""Полный WAL-safe бэкап SQLite перед миграциями при старте Жорика."""
import logging
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from config import settings

log = logging.getLogger(__name__)


def _open_source(source: Path):
    """Сначала read-only; при проблеме с WAL — rw без записи (backup() только читает)."""
    uri_ro = f"file:{source.resolve().as_posix()}?mode=ro"
    try:
        return sqlite3.connect(uri_ro, uri=True, timeout=30)
    except sqlite3.OperationalError as e:
        log.warning("mode=ro не открылся (%s), пробую mode=rw без записи", e)
        uri_rw = f"file:{source.resolve().as_posix()}?mode=rw"
        return sqlite3.connect(uri_rw, uri=True, timeout=30)


def create_full_backup(keep=14):
    if not isinstance(keep, int) or keep < 1:
        raise ValueError("keep должен быть положительным целым числом")
    source = Path(settings.db_path)
    if not source.is_file():
        raise FileNotFoundError(f"База SQLite не найдена: {source}")
    dest_dir = Path(settings.data_dir) / "db_backups"
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    target = dest_dir / f"zhorik_full_{stamp}.sqlite3"
    temp = dest_dir / f".zhorik_full_{stamp}.tmp"
    wal = source.with_name(source.name + "-wal")
    shm = source.with_name(source.name + "-shm")
    log.info("Бэкап SQLite: source=%s size=%d wal=%s shm=%s",
             source, source.stat().st_size, wal.exists(), shm.exists())
    try:
        with closing(_open_source(source)) as src:
            with closing(sqlite3.connect(str(temp), timeout=30)) as dst:
                src.backup(dst)
                check = dst.execute("PRAGMA integrity_check").fetchone()
                if not check or check[0] != "ok":
                    raise RuntimeError(f"Резервная копия повреждена: {check}")
        os.replace(temp, target)
        log.info("Создан полный резерв SQLite: %s (size=%d)", target, target.stat().st_size)
        for old in sorted(dest_dir.glob("zhorik_full_*.sqlite3"), reverse=True)[keep:]:
            try:
                old.unlink()
            except OSError:
                log.warning("Не удалось удалить старую копию: %s", old, exc_info=True)
        return target
    except Exception:
        temp.unlink(missing_ok=True)
        raise
