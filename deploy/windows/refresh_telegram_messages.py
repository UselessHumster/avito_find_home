import json
import re
import sqlite3
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))

from semi_manual_collector import create_listing_notifiers, load_config, notifier_for_score
from src.telegram_notifier import TelegramAPIError, build_listing_message

config = load_config(PROJECT / "config.yaml")
database = PROJECT / config["storage"]["database"]
notifiers = create_listing_notifiers(config)
with sqlite3.connect(database) as connection:
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        """
        SELECT * FROM listings
        WHERE telegram_notified_at IS NOT NULL AND telegram_message_ids IS NOT NULL
        ORDER BY score DESC, first_seen
        """
    ).fetchall()
requested_ids = set(sys.argv[1:])
if requested_ids:
    rows = [row for row in rows if row["id"] in requested_ids]

updated = 0
for row in rows:
    message_ids = json.loads(row["telegram_message_ids"] or "[]")
    if not message_ids:
        continue
    notifier = notifier_for_score(notifiers, int(row["score"] or 0))
    if notifier is None:
        continue
    details = json.loads(row["detail_data"] or "{}")
    text = build_listing_message(dict(row), details)
    while True:
        try:
            notifier.edit_listing_text(text, int(message_ids[0]))
            updated += 1
            break
        except TelegramAPIError as exc:
            error = str(exc)
            if "message is not modified" in error:
                updated += 1
                break
            if "message to edit not found" in error:
                new_ids = notifier.send_listing(text, [])
                with sqlite3.connect(database) as connection:
                    connection.execute(
                        "UPDATE listings SET telegram_message_ids = ? WHERE id = ?",
                        (json.dumps(new_ids), row["id"]),
                    )
                updated += 1
                break
            match = re.search(r"retry after (\d+)", error, re.IGNORECASE)
            if match:
                time.sleep(int(match.group(1)) + 1)
                continue
            print(f"FAILED={row['id']} {error}")
            break
    time.sleep(float(config["notifications"].get("send_delay_seconds", 3.2)))

print(f"UPDATED={updated}")
