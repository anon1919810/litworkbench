"""Offline backup/restore; never overwrites an existing directory."""
import argparse
import json

from litworkbench.backup import backup_workspace, restore_workspace
from litworkbench.errors import LitError
from litworkbench.store import Workspace


def main():
    parser = argparse.ArgumentParser(description='停写/停止 worker 后，备份或恢复到全新目录。')
    parser.add_argument('operation', choices=['backup', 'restore'])
    parser.add_argument('source'); parser.add_argument('destination')
    args = parser.parse_args()
    try:
        result = backup_workspace(Workspace(args.source), args.destination) if args.operation == 'backup' else restore_workspace(args.source, args.destination)
        print(json.dumps({'ok': True, 'file_count': len(result.get('files', [])), 'paused_jobs': len(result.get('paused_jobs', [])),
                          'requires_manual_preflight': result.get('requires_manual_preflight', False)}, ensure_ascii=False))
        return 0
    except LitError as exc:
        print(str(exc)); return 1


if __name__ == '__main__':
    raise SystemExit(main())
