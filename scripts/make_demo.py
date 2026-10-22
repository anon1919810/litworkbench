"""Generate explicitly synthetic fixtures, never actual historical material."""
import argparse
import json
from pathlib import Path

from litworkbench.demo import make_fixtures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--font", type=Path)
    args = parser.parse_args()
    result = make_fixtures(args.out, font=args.font)
    print(json.dumps({"synthetic": True, "files": result}, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
