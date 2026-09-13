import sqlite3
from pathlib import Path

import yaml

project = Path(__file__).resolve().parents[2]
with (project / "config.yaml").open(encoding="utf-8") as file:
    config = yaml.safe_load(file)

database = project / config["storage"]["database"]
with sqlite3.connect(database) as connection:
    urls = [row[0] for row in connection.execute("SELECT url FROM listings")]
    notified = connection.execute(
        "SELECT COUNT(*) FROM listings WHERE telegram_notified_at IS NOT NULL"
    ).fetchone()[0]

city_slug = config["city"]["slug"].strip("/")
allowed = tuple(
    f"/{city_slug}/{url.split(f'/{city_slug}/', 1)[1].split('/', 1)[0]}/"
    for search in config["city"]["searches"]
    if (url := search["url"]) and f"/{city_slug}/" in url
)
print(f"TOTAL={len(urls)}")
print(f"FOREIGN={sum(not any(part in url for part in allowed) for url in urls)}")
print(f"NOTIFIED={notified}")
