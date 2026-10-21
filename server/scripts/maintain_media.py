"""Bounded, user-scoped media maintenance with a persisted DynamoDB checkpoint."""

import argparse
import json

from app.services.media_service import MediaService
from app.services.storage import StorageError


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-sub", required=True)
    parser.add_argument("--limit", type=int, choices=range(1, 201), default=100, metavar="1..200")
    args = parser.parse_args()
    try:
        result = MediaService().maintain(args.user_sub, args.limit)
    except StorageError:
        parser.exit(1, "Media storage is unavailable. Retry this command.\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
