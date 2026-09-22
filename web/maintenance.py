"""Online backup and explicit retention, usable from cron or Windows Task Scheduler."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import uuid

from web.persistence import Database


def maintain(database, directory, keep_backups=7, keep_runs=None):
    if type(keep_backups) is not int or keep_backups < 1:
        raise ValueError('keep_backups must be positive')
    if keep_runs is not None and (type(keep_runs) is not int or keep_runs < 1):
        raise ValueError('keep_runs must be positive')
    directory = Path(directory)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backup = database.backup(directory / f'operator-{stamp}-{uuid.uuid4().hex[:8]}.sqlite3')
    removed = database.prune_runs(keep_runs) if keep_runs is not None else 0
    files = sorted(directory.glob('operator-*.sqlite3'), key=lambda path: path.name, reverse=True)
    for path in files[keep_backups:]:
        if path.resolve() != Path(database.path):
            path.unlink()
    return {'backup': backup, 'removed_runs': removed}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', default='results/operator.sqlite3')
    parser.add_argument('--directory', default='backups')
    parser.add_argument('--keep-backups', type=int, default=7)
    parser.add_argument('--keep-runs', type=int)
    args = parser.parse_args()
    if not Path(args.database).is_file():
        parser.error('Database does not exist')
    print(maintain(Database(args.database), args.directory, args.keep_backups, args.keep_runs))


if __name__ == '__main__':
    main()
