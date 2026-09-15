# -*- coding: utf-8 -*-
"""Avito long-term rental monitor.

The script launches a regular visible Chrome with a dedicated persistent
profile. A person handles login/challenges; the collector reads search results
and selectively opens the most promising listing pages for extra details.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import platform
import random
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import urlopen

import requests
import yaml
from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.service import Service as ChromeService

from src.listing_details import ListingDetails, parse_listing_details
from src.listing_rules import ListingEvaluation, evaluate_listing
from src.logger import logger
from src.search_results import SearchListing, page_is_blocked, parse_search_results
from src.telegram_notifier import (
    TelegramAPIError,
    TelegramNotifier,
    build_listing_message,
    load_env_file,
)


DEFAULT_CONFIG = "config.yaml"


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as file:
        config = yaml.safe_load(file)

    if not isinstance(config, dict):
        raise ValueError("Конфигурация должна быть YAML-объектом")
    if "city" in config:
        city = config.get("city") or {}
        rating = config.get("rating") or {}
        points = rating.get("points") or {}
        important = config.get("important") or {}
        price_colors = important.get("price_colors") or {}
        config["searches"] = city.get("searches", [])
        config["filters"] = config.get("hard_filters", {})
        config["ranking"] = {
            "minimum_area": rating.get("minimum_area", 54),
            "maximum_price": rating.get("maximum_real_price", 40_000),
            "positive_address_patterns": rating.get("good_streets", []),
            "negative_address_patterns": rating.get("bad_streets", []),
            "area_points": points.get("area", 1),
            "price_points": points.get("price", 1),
            "preferred_street_points": points.get("good_street", 2),
            "neutral_street_points": points.get("neutral_street", 1),
            "negative_street_points": points.get("bad_street", -1),
            "green_price_max": price_colors.get("green_max", 30_000),
            "yellow_price_max": price_colors.get("yellow_max", 40_000),
            "red_price_max": price_colors.get("red_max", 50_000),
            "show_children": important.get("children", True),
            "show_commission": important.get("commission", True),
            "show_utilities": important.get("utilities", True),
        }

    if not config.get("searches"):
        raise ValueError("В конфигурации отсутствует непустой список searches")
    return config


def find_chrome_executable(configured: str | None = None) -> str:
    candidates: list[str] = []
    if configured:
        candidates.append(os.path.expandvars(configured))

    system = platform.system()
    if system == "Windows":
        for env_name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = os.environ.get(env_name)
            if base:
                candidates.append(str(Path(base) / "Google/Chrome/Application/chrome.exe"))
    elif system == "Darwin":
        candidates.append("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    else:
        candidates.extend(filter(None, (shutil.which("google-chrome"), shutil.which("chromium"))))

    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    raise FileNotFoundError("Google Chrome не найден; задайте browser.chrome_path в конфигурации")


def port_is_open(port: int) -> bool:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def debugging_endpoint_ready(port: int) -> bool:
    try:
        with urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1) as response:
            return response.status == 200
    except Exception:
        return False


def launch_chrome(config: dict[str, Any], start_url: str) -> subprocess.Popen | None:
    browser = config["browser"]
    port = int(browser.get("debug_port", 9222))
    if debugging_endpoint_ready(port):
        logger.info(f"🌐 Используем уже открытый Chrome на порту {port}")
        return None
    if port_is_open(port):
        raise RuntimeError(f"Порт {port} занят другим процессом")

    executable = find_chrome_executable(browser.get("chrome_path"))
    profile_dir = Path(browser.get("profile_dir", "runtime/avito-chrome-profile")).resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)

    command = [
        executable,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        start_url,
    ]
    logger.info(f"🌐 Открываем обычный Chrome с профилем {profile_dir}")
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if debugging_endpoint_ready(port):
            return process
        if process.poll() is not None:
            raise RuntimeError(f"Chrome завершился с кодом {process.returncode}")
        time.sleep(0.5)
    raise TimeoutError("Chrome запущен, но порт удалённой отладки не открылся за 30 секунд")


def attach_driver(config: dict[str, Any]) -> webdriver.Chrome:
    port = int(config["browser"].get("debug_port", 9222))
    options = webdriver.ChromeOptions()
    options.add_experimental_option("debuggerAddress", f"127.0.0.1:{port}")
    options.page_load_strategy = "eager"

    configured_driver = os.environ.get("AVITO_CHROMEDRIVER") or config["browser"].get("driver_path")
    if configured_driver:
        driver_path = Path(os.path.expandvars(configured_driver)).resolve()
        if not driver_path.exists():
            raise FileNotFoundError(f"ChromeDriver не найден: {driver_path}")
        logger.info(f"Используем ChromeDriver: {driver_path}")
        driver = webdriver.Chrome(
            service=ChromeService(executable_path=str(driver_path)),
            options=options,
        )
        driver.set_page_load_timeout(int(config["collection"].get("page_load_timeout_seconds", 60)))
        return driver

    # Selenium prefers any chromedriver found in PATH, even when it is older
    # than the installed Chrome. Hide such directories for driver discovery so
    # Selenium Manager can resolve a matching version. Restore PATH immediately
    # afterwards because child processes outside this call may rely on it.
    original_path = os.environ.get("PATH", "")
    filtered_entries: list[str] = []
    for entry in original_path.split(os.pathsep):
        driver_names = ("chromedriver.exe",) if platform.system() == "Windows" else ("chromedriver",)
        if any((Path(entry) / name).exists() for name in driver_names):
            logger.warning(f"Игнорируем chromedriver из PATH при подборе версии: {entry}")
            continue
        filtered_entries.append(entry)
    os.environ["PATH"] = os.pathsep.join(filtered_entries)
    try:
        driver = webdriver.Chrome(options=options)
    finally:
        os.environ["PATH"] = original_path
    driver.set_page_load_timeout(int(config["collection"].get("page_load_timeout_seconds", 60)))
    return driver


def add_page_parameter(url: str, page: int) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    if page > 1:
        query["p"] = str(page)
    else:
        query.pop("p", None)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def stop_page_load(driver: webdriver.Chrome) -> None:
    try:
        driver.execute_script("window.stop();")
    except WebDriverException:
        pass


def dismiss_secret_homes_popup(driver: webdriver.Chrome) -> bool:
    """Close Avito's promotional “secret homes” dialog, if it is visible."""
    script = r"""
const visible = el => {
  const style = getComputedStyle(el);
  const rect = el.getBoundingClientRect();
  return style.display !== 'none' && style.visibility !== 'hidden'
    && rect.width > 0 && rect.height > 0;
};
const containers = [...document.querySelectorAll(
  '[role="dialog"], [data-marker*="modal"], [data-marker*="popup"], [class*="modal"], [class*="popup"]'
)].filter(visible);
for (const container of containers) {
  const text = (container.innerText || '').toLocaleLowerCase('ru-RU');
  const secret = text.includes('секретн');
  const homes = ['квартир', 'дом', 'жиль', 'объявлен'].some(word => text.includes(word));
  if (!secret || !homes) continue;
  const controls = [...container.querySelectorAll('button, [role="button"]')].filter(visible);
  const close = controls.find(control => {
    const label = [
      control.innerText,
      control.getAttribute('aria-label'),
      control.getAttribute('title'),
      control.getAttribute('data-marker'),
    ].filter(Boolean).join(' ').toLocaleLowerCase('ru-RU');
    return ['закры', 'close', 'крест', 'не сейчас', 'понятно'].some(word => label.includes(word))
      || ['×', '✕', '✖'].includes((control.innerText || '').trim());
  });
  if (close) {
    close.click();
    return true;
  }
}
return false;
"""
    try:
        dismissed = bool(driver.execute_script(script))
    except WebDriverException:
        return False
    if dismissed:
        logger.info("Закрыли рекламное окно Avito «секретные объявления»")
    return dismissed


def read_current_page(driver: webdriver.Chrome) -> tuple[str, str]:
    try:
        dismiss_secret_homes_popup(driver)
        return driver.page_source, driver.current_url
    except WebDriverException as exc:
        logger.warning(f"Не удалось прочитать страницу: {exc}")
        return "", ""


def wait_for_manual_access(
    driver: webdriver.Chrome,
    minutes: int,
    config: dict[str, Any],
) -> bool:
    logger.warning(
        "🧑 Нужна ручная проверка в окне Chrome. "
        "Войдите в аккаунт/пройдите проверку; сбор продолжится автоматически."
    )
    send_operational_alert(
        config,
        "captcha_search",
        "Авито требует ручную проверку",
        "Откройте Chrome на компьютере DESKTOP-KFE65RK и пройдите капчу или вход. Парсер ждёт и продолжит автоматически.",
    )
    deadline = time.monotonic() + minutes * 60
    while time.monotonic() < deadline:
        html, current_url = read_current_page(driver)
        if html and not page_is_blocked(html, current_url) and parse_search_results(html, "probe"):
            logger.success("✅ Поисковая выдача доступна")
            return True
        time.sleep(5)
    logger.error(f"Ручная проверка не завершена за {minutes} минут")
    send_operational_alert(
        config,
        "captcha_search_timeout",
        "Парсер не дождался прохождения капчи",
        f"Проверка в поисковой выдаче не завершена за {minutes} минут. Следующая попытка будет автоматически.",
    )
    return False


def wait_for_manual_unblock(
    driver: webdriver.Chrome,
    minutes: int,
    config: dict[str, Any],
) -> tuple[str, str] | None:
    logger.warning(
        "🧑 Авито остановил открытие карточки. Пройдите проверку в окне Chrome; "
        "после этого сборщик продолжит автоматически."
    )
    send_operational_alert(
        config,
        "captcha_detail",
        "Авито остановил открытие карточки",
        "Откройте Chrome на компьютере DESKTOP-KFE65RK и пройдите проверку. Парсер ждёт и продолжит автоматически.",
    )
    deadline = time.monotonic() + minutes * 60
    while time.monotonic() < deadline:
        html, current_url = read_current_page(driver)
        if html and not page_is_blocked(html, current_url):
            logger.success("✅ Проверка пройдена, карточка снова доступна")
            return html, current_url
        time.sleep(5)
    logger.error(f"Ручная проверка не завершена за {minutes} минут")
    send_operational_alert(
        config,
        "captcha_detail_timeout",
        "Карточки заблокированы капчей",
        f"Ручная проверка не завершена за {minutes} минут. Обогащение карточек отложено.",
    )
    return None


def navigate(driver: webdriver.Chrome, url: str) -> tuple[str, str]:
    try:
        driver.get(url)
    except TimeoutException:
        logger.warning("Страница не завершила загрузку в срок; останавливаем фоновые ресурсы")
        stop_page_load(driver)
    return read_current_page(driver)


def collect_search(
    driver: webdriver.Chrome,
    search: dict[str, str],
    collection: dict[str, Any],
    config: dict[str, Any],
    on_page: Callable[[list[SearchListing]], None] | None = None,
) -> tuple[list[SearchListing], bool]:
    category = search["name"]
    max_pages = int(collection.get("max_pages_per_search", 20))
    manual_wait = int(collection.get("manual_wait_minutes", 15))
    delay_min = float(collection.get("page_delay_seconds_min", 12))
    delay_max = float(collection.get("page_delay_seconds_max", 25))
    collected: dict[str, SearchListing] = {}
    completed = False
    search_parts = [part for part in urlsplit(search["url"]).path.split("/") if part]
    expected_path_prefix = (
        f"/{search_parts[0]}/{search_parts[1]}/" if len(search_parts) >= 2 else ""
    )

    for page in range(1, max_pages + 1):
        page_url = add_page_parameter(search["url"], page)
        logger.info(f"🔎 {category}: страница {page}")
        html, current_url = navigate(driver, page_url)

        if page_is_blocked(html, current_url):
            if not wait_for_manual_access(driver, manual_wait, config):
                return list(collected.values()), False
            html, current_url = read_current_page(driver)

        parsed_listings = parse_search_results(html, category)
        page_listings = [
            listing
            for listing in parsed_listings
            if not expected_path_prefix
            or urlsplit(listing.url).path.startswith(expected_path_prefix)
        ]
        foreign_count = len(parsed_listings) - len(page_listings)
        if foreign_count:
            logger.info(
                f"{category}: пропущено рекомендаций вне Ессентуков: {foreign_count}"
            )
        if not page_listings:
            logger.info(
                f"{category}: местных карточек на странице {page} нет, категория завершена"
            )
            completed = True
            break

        new_count = 0
        for listing in page_listings:
            if listing.id not in collected:
                collected[listing.id] = listing
                new_count += 1
        logger.info(f"{category}: карточек {len(page_listings)}, новых в проходе {new_count}")
        if on_page:
            on_page(page_listings)

        if new_count == 0:
            completed = True
            break
        if page < max_pages:
            time.sleep(random.uniform(delay_min, delay_max))
    else:
        logger.warning(f"{category}: достигнут защитный лимит {max_pages} страниц")

    return list(collected.values()), completed


SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    id TEXT PRIMARY KEY,
    category TEXT NOT NULL,
    url TEXT NOT NULL,
    title TEXT,
    price INTEGER,
    price_text TEXT,
    address TEXT,
    params TEXT,
    published_text TEXT,
    description_snippet TEXT,
    raw_text TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    rooms INTEGER,
    area REAL,
    rooms_detail INTEGER,
    area_detail REAL,
    is_short_term INTEGER NOT NULL DEFAULT 0,
    hard_filter_passed INTEGER NOT NULL DEFAULT 0,
    filter_reason TEXT,
    room_check_pending INTEGER NOT NULL DEFAULT 0,
    score INTEGER NOT NULL DEFAULT 0,
    score_reasons TEXT NOT NULL DEFAULT '{}',
    preferred_area_match INTEGER NOT NULL DEFAULT 0,
    negative_area_match INTEGER NOT NULL DEFAULT 0,
    detail_status TEXT NOT NULL DEFAULT 'not_requested',
    detail_last_attempt TEXT,
    detail_fetched_at TEXT,
    detail_data TEXT,
    photo_urls TEXT,
    photo_paths TEXT,
    telegram_notified_at TEXT,
    telegram_message_ids TEXT,
    telegram_last_attempt TEXT,
    telegram_error TEXT,
    telegram_enriched_at TEXT
)
"""


MIGRATION_COLUMNS = {
    "rooms": "INTEGER",
    "area": "REAL",
    "rooms_detail": "INTEGER",
    "area_detail": "REAL",
    "is_short_term": "INTEGER NOT NULL DEFAULT 0",
    "hard_filter_passed": "INTEGER NOT NULL DEFAULT 0",
    "filter_reason": "TEXT",
    "room_check_pending": "INTEGER NOT NULL DEFAULT 0",
    "score": "INTEGER NOT NULL DEFAULT 0",
    "score_reasons": "TEXT NOT NULL DEFAULT '{}'",
    "preferred_area_match": "INTEGER NOT NULL DEFAULT 0",
    "negative_area_match": "INTEGER NOT NULL DEFAULT 0",
    "detail_status": "TEXT NOT NULL DEFAULT 'not_requested'",
    "detail_last_attempt": "TEXT",
    "detail_fetched_at": "TEXT",
    "detail_data": "TEXT",
    "photo_urls": "TEXT",
    "photo_paths": "TEXT",
    "telegram_notified_at": "TEXT",
    "telegram_message_ids": "TEXT",
    "telegram_last_attempt": "TEXT",
    "telegram_error": "TEXT",
    "telegram_enriched_at": "TEXT",
}


def ensure_schema(connection: sqlite3.Connection) -> None:
    connection.execute(SCHEMA)
    existing = {row[1] for row in connection.execute("PRAGMA table_info(listings)")}
    for column, definition in MIGRATION_COLUMNS.items():
        if column not in existing:
            connection.execute(f"ALTER TABLE listings ADD COLUMN {column} {definition}")


def _evaluate_row(
    row: sqlite3.Row,
    filters: dict[str, Any],
    ranking: dict[str, Any],
) -> ListingEvaluation:
    detail_parameters: list[str] = []
    try:
        detail_payload = json.loads(row["detail_data"] or "{}")
        if isinstance(detail_payload.get("parameters"), list):
            detail_parameters = [str(value) for value in detail_payload["parameters"]]
    except (json.JSONDecodeError, TypeError, AttributeError):
        pass
    return evaluate_listing(
        category=row["category"],
        title=row["title"],
        price=row["price"],
        price_text=row["price_text"],
        address=row["address"],
        params=row["params"],
        raw_text=row["raw_text"],
        minimum_rooms=int(filters.get("minimum_rooms", 2)),
        exclude_short_term=bool(filters.get("exclude_short_term", True)),
        minimum_area=float(ranking.get("minimum_area", 54)),
        maximum_price=int(ranking.get("maximum_price", 40_000)),
        preferred_address_patterns=ranking.get(
            "positive_address_patterns",
            ranking.get("preferred_address_patterns", []),
        ),
        negative_address_patterns=ranking.get("negative_address_patterns", []),
        detail_rooms=row["rooms_detail"],
        detail_area=row["area_detail"],
        detail_parameters=detail_parameters,
        area_points=int(ranking.get("area_points", 1)),
        price_points=int(ranking.get("price_points", 1)),
        preferred_street_points=int(ranking.get("preferred_street_points", 2)),
        neutral_street_points=int(ranking.get("neutral_street_points", 1)),
        negative_street_points=int(ranking.get("negative_street_points", -1)),
        green_price_max=int(ranking.get("green_price_max", 30_000)),
        red_price_max=int(ranking.get("red_price_max", 50_000)),
        show_children=bool(ranking.get("show_children", True)),
        show_commission=bool(ranking.get("show_commission", True)),
        show_utilities=bool(ranking.get("show_utilities", True)),
    )


def _save_evaluation(
    connection: sqlite3.Connection,
    listing_id: str,
    evaluation: ListingEvaluation,
) -> None:
    connection.execute(
        """
        UPDATE listings SET
            rooms = ?, area = ?, is_short_term = ?, hard_filter_passed = ?,
            filter_reason = ?, room_check_pending = ?, score = ?,
            score_reasons = ?, preferred_area_match = ?, negative_area_match = ?
        WHERE id = ?
        """,
        (
            evaluation.rooms,
            evaluation.area,
            int(evaluation.is_short_term),
            int(evaluation.hard_filter_passed),
            evaluation.filter_reason,
            int(evaluation.room_check_pending),
            evaluation.score,
            json.dumps(evaluation.score_reasons, ensure_ascii=False, sort_keys=True),
            int(evaluation.preferred_area_match),
            int(evaluation.negative_area_match),
            listing_id,
        ),
    )


def refresh_evaluations(
    connection: sqlite3.Connection,
    filters: dict[str, Any],
    ranking: dict[str, Any],
    listing_ids: list[str] | None = None,
) -> None:
    connection.row_factory = sqlite3.Row
    if listing_ids:
        placeholders = ",".join("?" for _ in listing_ids)
        rows = connection.execute(
            f"SELECT * FROM listings WHERE id IN ({placeholders})",
            listing_ids,
        ).fetchall()
    else:
        rows = connection.execute("SELECT * FROM listings").fetchall()
    for row in rows:
        _save_evaluation(connection, row["id"], _evaluate_row(row, filters, ranking))


def prepare_database(config: dict[str, Any]) -> None:
    database_path = Path(config["storage"]["database"])
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(database_path)) as connection, connection:
        ensure_schema(connection)
        refresh_evaluations(connection, config["filters"], config["ranking"])


def save_listings(
    database_path: Path,
    listings: list[SearchListing],
    completed: bool,
    category: str,
    filters: dict[str, Any],
    ranking: dict[str, Any],
) -> None:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    with closing(sqlite3.connect(database_path)) as connection, connection:
        ensure_schema(connection)
        if completed:
            connection.execute("UPDATE listings SET is_active = 0 WHERE category = ?", (category,))
        for listing in listings:
            values = listing.to_dict()
            connection.execute(
                """
                INSERT INTO listings (
                    id, category, url, title, price, price_text, address, params,
                    published_text, description_snippet, raw_text,
                    first_seen, last_seen, is_active
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(id) DO UPDATE SET
                    category = excluded.category,
                    url = excluded.url,
                    title = excluded.title,
                    price = excluded.price,
                    price_text = excluded.price_text,
                    address = excluded.address,
                    params = excluded.params,
                    published_text = excluded.published_text,
                    description_snippet = excluded.description_snippet,
                    raw_text = excluded.raw_text,
                    last_seen = excluded.last_seen,
                    is_active = 1
                """,
                (
                    values["id"], values["category"], values["url"], values["title"],
                    values["price"], values["price_text"], values["address"], values["params"],
                    values["published_text"], values["description_snippet"], values["raw_text"],
                    now, now,
                ),
            )
        refresh_evaluations(
            connection,
            filters,
            ranking,
            [listing.id for listing in listings],
        )


def export_csv(database_path: Path, csv_path: Path, candidates_only: bool = False) -> int:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(database_path)) as connection, connection:
        connection.row_factory = sqlite3.Row
        where = "WHERE is_active = 1 AND hard_filter_passed = 1" if candidates_only else ""
        cursor = connection.execute(
            f"SELECT * FROM listings {where} "
            "ORDER BY is_active DESC, score DESC, first_seen DESC, category, id"
        )
        fieldnames = [column[0] for column in cursor.description]
        rows = cursor.fetchall()
    with csv_path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(dict(row) for row in rows)
    return len(rows)


def export_outputs(config: dict[str, Any]) -> tuple[int, int]:
    storage = config["storage"]
    database_path = Path(storage["database"])
    all_count = export_csv(database_path, Path(storage["csv"]))
    candidate_count = export_csv(
        database_path,
        Path(storage.get("candidates_csv", "output/essentuki_candidates.csv")),
        candidates_only=True,
    )
    return all_count, candidate_count


def select_detail_candidates(
    database_path: Path,
    minimum_score: int,
    limit: int,
) -> list[sqlite3.Row]:
    with closing(sqlite3.connect(database_path)) as connection, connection:
        connection.row_factory = sqlite3.Row
        return connection.execute(
            """
            SELECT id, url, category, title, score, room_check_pending
            FROM listings
            WHERE is_active = 1
              AND hard_filter_passed = 1
              AND score >= ?
              AND detail_status IN ('not_requested', 'error')
            ORDER BY last_seen DESC, score DESC, first_seen DESC
            LIMIT ?
            """,
            (minimum_score, limit),
        ).fetchall()


def record_detail_failure(database_path: Path, listing_id: str, status: str) -> None:
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    with closing(sqlite3.connect(database_path)) as connection, connection:
        ensure_schema(connection)
        connection.execute(
            "UPDATE listings SET detail_status = ?, detail_last_attempt = ? WHERE id = ?",
            (status, now, listing_id),
        )


def save_listing_details(
    database_path: Path,
    listing_id: str,
    details: ListingDetails,
    photo_paths: list[str],
    filters: dict[str, Any],
    ranking: dict[str, Any],
) -> ListingEvaluation:
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    with closing(sqlite3.connect(database_path)) as connection, connection:
        connection.row_factory = sqlite3.Row
        ensure_schema(connection)
        connection.execute(
            """
            UPDATE listings SET
                rooms_detail = ?, area_detail = ?, detail_status = 'success',
                detail_last_attempt = ?, detail_fetched_at = ?, detail_data = ?,
                photo_urls = ?, photo_paths = ?
            WHERE id = ?
            """,
            (
                details.rooms,
                details.area,
                now,
                now,
                json.dumps(details.to_dict(), ensure_ascii=False),
                json.dumps(details.photo_urls, ensure_ascii=False),
                json.dumps(photo_paths, ensure_ascii=False),
                listing_id,
            ),
        )
        row = connection.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone()
        if row is None:
            raise RuntimeError(f"Объявление исчезло из базы: {listing_id}")
        evaluation = _evaluate_row(row, filters, ranking)
        _save_evaluation(connection, listing_id, evaluation)
        return evaluation


def download_listing_photos(
    driver: webdriver.Chrome,
    listing_id: str,
    listing_url: str,
    photo_urls: list[str],
    photos_root: Path,
) -> list[str]:
    if not photo_urls:
        return []

    target_dir = photos_root / listing_id
    target_dir.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    for cookie in driver.get_cookies():
        cookie_options = {"domain": cookie["domain"]} if cookie.get("domain") else {}
        session.cookies.set(cookie["name"], cookie["value"], **cookie_options)
    try:
        user_agent = driver.execute_script("return navigator.userAgent")
    except WebDriverException:
        user_agent = "Mozilla/5.0"
    headers = {"User-Agent": user_agent, "Referer": listing_url}

    saved: list[str] = []
    suffix_by_type = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/avif": ".avif",
    }
    for index, photo_url in enumerate(photo_urls, 1):
        temporary_path: Path | None = None
        try:
            with session.get(photo_url, headers=headers, timeout=30, stream=True) as response:
                response.raise_for_status()
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
                if content_type and not content_type.startswith("image/"):
                    raise ValueError(f"ожидалось изображение, получено {content_type}")
                suffix = suffix_by_type.get(content_type, ".jpg")
                destination = target_dir / f"{index:02d}{suffix}"
                temporary_path = destination.with_suffix(destination.suffix + ".part")
                downloaded = 0
                with temporary_path.open("wb") as file:
                    for chunk in response.iter_content(chunk_size=64 * 1024):
                        if not chunk:
                            continue
                        downloaded += len(chunk)
                        if downloaded > 20 * 1024 * 1024:
                            raise ValueError("изображение больше 20 МБ")
                        file.write(chunk)
                temporary_path.replace(destination)
                saved.append(str(destination))
        except (OSError, requests.RequestException, ValueError) as exc:
            if temporary_path and temporary_path.exists():
                temporary_path.unlink()
            logger.warning(f"Фото {index} для {listing_id} не сохранено: {exc}")
    return saved


def enrich_candidates(driver: webdriver.Chrome, config: dict[str, Any]) -> int:
    details_config = config.get("details", {})
    if not details_config.get("enabled", False):
        return 0

    storage = config["storage"]
    database_path = Path(storage["database"])
    minimum_score = int(details_config.get("minimum_score", 3))
    max_per_cycle = int(details_config.get("max_per_cycle", 3))
    candidates = select_detail_candidates(database_path, minimum_score, max_per_cycle)
    if not candidates:
        logger.info(f"Карточек с рейтингом {minimum_score}+ для дообогащения нет")
        return 0

    logger.info(
        f"⭐ Откроем не более {len(candidates)} карточек с рейтингом {minimum_score}+ "
        "и остановимся при блокировке"
    )
    max_photos = int(details_config.get("max_photos", 3))
    download_photos = bool(details_config.get("download_photos", True))
    delay_min = float(details_config.get("page_delay_seconds_min", 30))
    delay_max = float(details_config.get("page_delay_seconds_max", 45))
    manual_wait = int(config["collection"].get("manual_wait_minutes", 15))
    photos_root = Path(storage.get("photos_dir", "output/photos"))
    enriched = 0

    for index, candidate in enumerate(candidates, 1):
        listing_id = candidate["id"]
        logger.info(
            f"🏠 Карточка {index}/{len(candidates)}: {listing_id}, "
            f"рейтинг {candidate['score']}"
        )
        html, current_url = navigate(driver, candidate["url"])
        if page_is_blocked(html, current_url):
            resumed = wait_for_manual_unblock(driver, manual_wait, config)
            if resumed is None:
                record_detail_failure(database_path, listing_id, "blocked")
                break
            html, current_url = resumed

        details = parse_listing_details(html, max_photos=max_photos)
        if not any((details.title, details.description, details.parameters)):
            logger.warning(f"Карточка {listing_id} загрузилась без распознаваемых деталей")
            record_detail_failure(database_path, listing_id, "error")
            continue

        photo_paths = []
        if download_photos:
            photo_paths = download_listing_photos(
                driver,
                listing_id,
                candidate["url"],
                details.photo_urls,
                photos_root,
            )
        evaluation = save_listing_details(
            database_path,
            listing_id,
            details,
            photo_paths,
            config["filters"],
            config["ranking"],
        )
        enriched += 1
        logger.info(
            f"💾 Детали сохранены: комнат={details.rooms or 'неизвестно'}, "
            f"фото={len(photo_paths)}, итоговый фильтр="
            f"{'пройден' if evaluation.hard_filter_passed and not evaluation.room_check_pending else 'не пройден'}"
        )
        if index < len(candidates):
            time.sleep(random.uniform(delay_min, delay_max))
    return enriched


def create_telegram_notifier(
    config: dict[str, Any],
    chat_id_env: str = "TELEGRAM_CHAT_ID",
) -> TelegramNotifier | None:
    notification_config = config.get("notifications", {})
    if not notification_config.get("enabled", False):
        return None
    load_env_file(Path(notification_config.get("env_file", ".env")))
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get(chat_id_env, "").strip()
    if not token or not chat_id:
        logger.warning(
            f"Telegram не настроен: заполните TELEGRAM_BOT_TOKEN и {chat_id_env} в .env"
        )
        return None
    return TelegramNotifier(
        token=token,
        chat_id=chat_id,
        timeout=float(notification_config.get("request_timeout_seconds", 30)),
    )


def create_listing_notifiers(config: dict[str, Any]) -> dict[str, TelegramNotifier | None]:
    main = create_telegram_notifier(config, "TELEGRAM_CHAT_ID")
    priority = create_telegram_notifier(config, "TELEGRAM_PRIORITY_CHAT_ID") or main
    trash = create_telegram_notifier(config, "TELEGRAM_TRASH_CHAT_ID") or main
    return {"main": main, "priority": priority, "trash": trash}


def create_technical_notifier(config: dict[str, Any]) -> TelegramNotifier | None:
    return (
        create_telegram_notifier(config, "TELEGRAM_TECH_CHAT_ID")
        or create_telegram_notifier(config, "TELEGRAM_CHAT_ID")
    )


def notifier_for_score(
    notifiers: dict[str, TelegramNotifier | None], score: int
) -> TelegramNotifier | None:
    if score >= 3:
        return notifiers["priority"]
    if score <= 1:
        return notifiers["trash"]
    return notifiers["main"]


def send_operational_alert(
    config: dict[str, Any],
    event_key: str,
    title: str,
    message: str,
    cooldown_seconds: int = 1800,
) -> bool:
    """Send an important alert, suppressing repetitions across restarts."""
    state_path = Path("runtime/telegram_alert_state.json")
    now = time.time()
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        state = {}
    last_sent = float(state.get(event_key, 0) or 0)
    if now - last_sent < cooldown_seconds:
        return False
    notifier = create_technical_notifier(config)
    if notifier is None:
        return False
    try:
        notifier.send_alert(title, message)
    except TelegramAPIError as exc:
        logger.error(f"Telegram: служебный алерт не отправлен: {exc}")
        return False
    state[event_key] = now
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    return True


def select_notification_candidates(
    database_path: Path,
    minimum_score: int,
    limit: int,
) -> list[sqlite3.Row]:
    with closing(sqlite3.connect(database_path)) as connection, connection:
        connection.row_factory = sqlite3.Row
        ensure_schema(connection)
        return connection.execute(
            """
            SELECT * FROM listings
            WHERE is_active = 1
              AND hard_filter_passed = 1
              AND room_check_pending = 0
              AND telegram_notified_at IS NULL
            ORDER BY score DESC, first_seen
            LIMIT ?
            """,
            (limit,),
        ).fetchall()


def select_enrichment_notifications(
    database_path: Path,
    minimum_score: int,
    limit: int,
) -> list[sqlite3.Row]:
    with closing(sqlite3.connect(database_path)) as connection, connection:
        connection.row_factory = sqlite3.Row
        ensure_schema(connection)
        return connection.execute(
            """
            SELECT * FROM listings
            WHERE is_active = 1
              AND hard_filter_passed = 1
              AND room_check_pending = 0
              AND score >= ?
              AND detail_status = 'success'
              AND telegram_notified_at IS NOT NULL
              AND telegram_enriched_at IS NULL
            ORDER BY detail_fetched_at, score DESC
            LIMIT ?
            """,
            (minimum_score, limit),
        ).fetchall()


def record_telegram_result(
    database_path: Path,
    listing_id: str,
    *,
    message_ids: list[int] | None = None,
    error: str | None = None,
) -> None:
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    message_ids_json = json.dumps(message_ids) if message_ids is not None else None
    notified_at = now if message_ids is not None else None
    with closing(sqlite3.connect(database_path)) as connection, connection:
        ensure_schema(connection)
        connection.execute(
            """
            UPDATE listings SET
                telegram_last_attempt = ?,
                telegram_notified_at = COALESCE(?, telegram_notified_at),
                telegram_message_ids = COALESCE(?, telegram_message_ids),
                telegram_error = ?
            WHERE id = ?
            """,
            (
                now,
                notified_at,
                message_ids_json,
                error,
                listing_id,
            ),
        )


def send_pending_notifications(config: dict[str, Any]) -> int:
    notifiers = create_listing_notifiers(config)

    notification_config = config["notifications"]
    database_path = Path(config["storage"]["database"])
    candidates = select_notification_candidates(
        database_path,
        int(notification_config.get("minimum_score", 3)),
        int(notification_config.get("max_per_cycle", 10)),
    )
    sent = 0
    send_delay = float(notification_config.get("send_delay_seconds", 3.2))
    for index, candidate in enumerate(candidates):
        listing = dict(candidate)
        notifier = notifier_for_score(notifiers, int(candidate["score"] or 0))
        if notifier is None:
            continue
        try:
            details = json.loads(candidate["detail_data"] or "{}")
        except (json.JSONDecodeError, TypeError):
            details = {}
        try:
            saved_paths = json.loads(candidate["photo_paths"] or "[]")
        except (json.JSONDecodeError, TypeError):
            saved_paths = []
        text = build_listing_message(listing, details)
        message_ids = None
        transient_attempts = 0
        while True:
            try:
                message_ids = notifier.send_listing(text, [Path(path) for path in saved_paths])
                break
            except TelegramAPIError as exc:
                error = str(exc)[:500]
                retry_match = re.search(r"retry after (\d+)", error, re.IGNORECASE)
                if retry_match:
                    retry_seconds = int(retry_match.group(1)) + 1
                    logger.warning(
                        f"Telegram просит паузу {retry_seconds} с; после неё повторим {candidate['id']}"
                    )
                    time.sleep(retry_seconds)
                    continue
                transient_attempts += 1
                if transient_attempts < 3 and "request failed" in error.casefold():
                    retry_seconds = 5 * transient_attempts
                    logger.warning(
                        f"Временная ошибка Telegram; повторим {candidate['id']} через {retry_seconds} с"
                    )
                    time.sleep(retry_seconds)
                    continue
                record_telegram_result(database_path, candidate["id"], error=error)
                logger.error(f"Telegram: объявление {candidate['id']} не отправлено: {error}")
                break
        if message_ids is None:
            continue
        record_telegram_result(database_path, candidate["id"], message_ids=message_ids)
        if int(candidate["score"] or 0) >= 3 and candidate["detail_status"] == "success":
            now = datetime.now().astimezone().isoformat(timespec="seconds")
            with closing(sqlite3.connect(database_path)) as connection, connection:
                connection.execute(
                    "UPDATE listings SET telegram_enriched_at = ? WHERE id = ?",
                    (now, candidate["id"]),
                )
        sent += 1
        logger.success(f"📨 Telegram: отправлено объявление {candidate['id']}")
        if index + 1 < len(candidates):
            time.sleep(send_delay)
    return sent


def send_enrichment_notifications(config: dict[str, Any]) -> int:
    notifier = create_listing_notifiers(config)["priority"]
    if notifier is None:
        return 0
    notification_config = config["notifications"]
    database_path = Path(config["storage"]["database"])
    candidates = select_enrichment_notifications(
        database_path,
        int(notification_config.get("minimum_score", 3)),
        int(notification_config.get("max_per_cycle", 10)),
    )
    updated = 0
    for candidate in candidates:
        try:
            details = json.loads(candidate["detail_data"] or "{}")
            saved_paths = json.loads(candidate["photo_paths"] or "[]")
            original_ids = json.loads(candidate["telegram_message_ids"] or "[]")
            message_ids = notifier.enrich_listing(
                build_listing_message(dict(candidate), details),
                [int(value) for value in original_ids],
                [Path(path) for path in saved_paths],
            )
        except (TelegramAPIError, json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.error(f"Telegram: не удалось дополнить {candidate['id']}: {exc}")
            continue
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with closing(sqlite3.connect(database_path)) as connection, connection:
            connection.execute(
                """
                UPDATE listings SET telegram_enriched_at = ?, telegram_message_ids = ?,
                    telegram_error = NULL
                WHERE id = ?
                """,
                (now, json.dumps(message_ids), candidate["id"]),
            )
        updated += 1
        logger.success(f"📨 Telegram: дополнено объявление {candidate['id']}")
    return updated


def run_cycle(driver: webdriver.Chrome, config: dict[str, Any]) -> dict[str, int]:
    storage = config["storage"]
    database_path = Path(storage["database"])
    with closing(sqlite3.connect(database_path)) as connection:
        known_ids = {row[0] for row in connection.execute("SELECT id FROM listings")}
    seen_ids: set[str] = set()
    total_seen = 0
    for search in config["searches"]:
        category = search["name"]
        listings, completed = collect_search(
            driver,
            search,
            config["collection"],
            config,
            on_page=lambda page_items, current_category=category: save_listings(
                database_path,
                page_items,
                False,
                current_category,
                config["filters"],
                config["ranking"],
            ),
        )
        save_listings(
            database_path,
            listings,
            completed,
            search["name"],
            config["filters"],
            config["ranking"],
        )
        total_seen += len(listings)
        seen_ids.update(listing.id for listing in listings)
        logger.info(
            f"💾 {search['name']}: сохранено {len(listings)}; "
            f"полный проход={'да' if completed else 'нет'}"
        )
    notifications_sent = send_pending_notifications(config)
    enriched = enrich_candidates(driver, config)
    notifications_enriched = send_enrichment_notifications(config)
    total_stored, filtered_count = export_outputs(config)
    logger.success(
        f"✅ Цикл завершён: увидено {total_seen}, всего в базе {total_stored}, "
        f"прошли первичные фильтры {filtered_count}, карточек обогащено {enriched}, "
        f"уведомлений отправлено {notifications_sent}, дополнено {notifications_enriched}"
    )
    return {
        "added": len(seen_ids - known_ids),
        "updated": len(seen_ids & known_ids),
        "seen": len(seen_ids),
        "enriched": enriched,
    }


def send_cycle_report(
    config: dict[str, Any],
    stats: dict[str, int],
    next_run: datetime | None,
) -> None:
    notifier = create_technical_notifier(config)
    if notifier is None:
        return
    next_text = (
        next_run.strftime("%d.%m.%Y в %H:%M")
        if next_run is not None
        else "не запланирован (--once)"
    )
    message = (
        "✅ <b>Прогон завершён</b>\n\n"
        f"🆕 Добавлено: <b>{stats['added']}</b>\n"
        f"🔄 Обновлено: <b>{stats['updated']}</b>\n"
        f"👀 Найдено в выдаче: <b>{stats['seen']}</b>\n"
        f"🏠 Карточек дополнено: <b>{stats['enriched']}</b>\n"
        f"⏰ Следующий прогон: <b>{next_text}</b>"
    )
    try:
        notifier.send_listing(message, [])
    except TelegramAPIError as exc:
        logger.error(f"Telegram: отчёт о прогоне не отправлен: {exc}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Полуручной сбор аренды из поисковой выдачи Авито")
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="Путь к YAML-конфигурации")
    parser.add_argument("--once", action="store_true", help="Выполнить один цикл и завершиться")
    parser.add_argument(
        "--details-only",
        action="store_true",
        help="Не обновлять выдачу, обработать только уже найденные карточки с высоким рейтингом",
    )
    parser.add_argument(
        "--no-details",
        action="store_true",
        help="Собрать только выдачу и не открывать карточки в этом запуске",
    )
    parser.add_argument(
        "--max-details",
        type=int,
        help="Переопределить максимальное число открываемых карточек в этом запуске",
    )
    parser.add_argument(
        "--notify-only",
        action="store_true",
        help="Отправить ожидающие Telegram-уведомления без запуска Chrome",
    )
    parser.add_argument(
        "--search",
        action="append",
        help="Собрать только указанную категорию; параметр можно повторять",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_path = Path(args.config)
    if not config_path.exists():
        logger.error(f"Конфигурация не найдена: {config_path}")
        return 2

    config = load_config(config_path)
    if args.search:
        requested = set(args.search)
        config["searches"] = [search for search in config["searches"] if search["name"] in requested]
        missing = requested - {search["name"] for search in config["searches"]}
        if missing:
            logger.error(f"Неизвестные категории: {', '.join(sorted(missing))}")
            return 2
    prepare_database(config)
    export_outputs(config)
    if args.no_details:
        config.setdefault("details", {})["enabled"] = False
    if args.max_details is not None:
        if args.max_details < 0:
            logger.error("--max-details не может быть отрицательным")
            return 2
        config.setdefault("details", {})["max_per_cycle"] = args.max_details
    if args.notify_only:
        sent = send_pending_notifications(config)
        updated = send_enrichment_notifications(config)
        logger.success(
            f"📨 Ожидающих Telegram-уведомлений отправлено: {sent}; дополнено: {updated}"
        )
        return 0
    first_url = config["searches"][0]["url"]
    launch_chrome(config, first_url)
    driver = attach_driver(config)

    try:
        if args.details_only:
            notifications_sent = send_pending_notifications(config)
            enriched = enrich_candidates(driver, config)
            notifications_enriched = send_enrichment_notifications(config)
            total_stored, filtered_count = export_outputs(config)
            logger.success(
                f"✅ Обогащено карточек: {enriched}; всего в базе {total_stored}; "
                f"прошли первичные фильтры {filtered_count}; "
                f"уведомлений отправлено {notifications_sent}; "
                f"дополнено {notifications_enriched}"
            )
            return 0
        while True:
            stats = run_cycle(driver, config)
            if args.once:
                send_cycle_report(config, stats, None)
                break
            interval_min = int(config["collection"].get("interval_minutes_min", 30))
            interval_max = int(config["collection"].get("interval_minutes_max", 60))
            delay_minutes = random.randint(interval_min, interval_max)
            next_run = datetime.now().astimezone() + timedelta(minutes=delay_minutes)
            logger.info(f"⏳ Следующий цикл через {delay_minutes} минут")
            send_cycle_report(config, stats, next_run)
            time.sleep(delay_minutes * 60)
    except KeyboardInterrupt:
        storage = config["storage"]
        database_path = Path(storage["database"])
        if database_path.exists():
            total_stored, filtered_count = export_outputs(config)
            logger.info(
                f"💾 Перед остановкой выгружено записей: {total_stored}; "
                f"прошли первичные фильтры: {filtered_count}"
            )
        logger.info("Остановлено пользователем; окно Chrome и профиль сохранены")
    finally:
        # Stop only the ChromeDriver bridge. The visible Chrome remains open so
        # its authenticated profile and manually solved challenge survive.
        try:
            driver.service.stop()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        logger.exception(f"Критическая ошибка парсера: {exc}")
        try:
            emergency_config = load_config(Path(DEFAULT_CONFIG))
            send_operational_alert(
                emergency_config,
                f"fatal:{type(exc).__name__}",
                "Парсер аварийно остановился",
                f"{type(exc).__name__}: {str(exc)[:1200]}\n\nАвтозапуск попробует поднять процесс снова через минуту.",
                cooldown_seconds=900,
            )
        except Exception as alert_exc:
            logger.error(f"Не удалось отправить аварийный алерт: {alert_exc}")
        raise
