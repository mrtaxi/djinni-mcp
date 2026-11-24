#!/usr/bin/env python3
"""Djinni freshness scan — anonymous, no cookies, no account.

Reads public pages only:
  * https://djinni.co/jobs/?all_keywords=<query>&page=N   (listing, JSON-LD)
  * https://djinni.co/jobs/<id>-<slug>/                   (publication dates)

Two separate questions get two separate answers:

  * "have I already seen this vacancy?" — by vacancy id. A bumped vacancy keeps
    its id, so a local seen-store answers this for free, without fetching it.
  * "is this advertisement actually old?" — by published_at vs updated_at, which
    only the job page exposes. JSON-LD `datePosted` is the *update* date, so it
    must not be used for freshness.

Usage:
    python fresh_jobs.py                          # default query, 2 pages
    python fresh_jobs.py -q "DevOps" -p 2 -c 5
    python fresh_jobs.py --json                   # machine-readable
    python fresh_jobs.py --reset-seen             # forget the seen-store
    python fresh_jobs.py --seed-from-inbox 2      # needs the account: mark applied jobs as seen
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(HERE, ".state")
SEEN_PATH = os.path.join(STATE_DIR, "seen_jobs.json")
LEDGER_PATH = os.path.join(STATE_DIR, "fresh_ledger.json")

BASE = "https://djinni.co"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:132.0) Gecko/20100101 Firefox/132.0"
DEFAULT_CAP = 60          # anonymous requests per day
DEFAULT_PACE = (3.0, 5.0)  # seconds between requests
KEEP_DAYS = 14

sys.path.insert(0, HERE)
try:
    from server import _postings, _publication  # single source of truth for parsing
except Exception as exc:  # pragma: no cover
    raise SystemExit(f"cannot import parsers from server.py: {exc}")


# ------------------------------------------------------------------ budget ---

class Budget:
    """Anonymous request budget, separate from the MCP server's account ledger."""

    def __init__(self, cap: int):
        self.cap = cap
        os.makedirs(STATE_DIR, exist_ok=True)
        self.data = {"days": {}}
        if os.path.isfile(LEDGER_PATH):
            try:
                self.data = json.load(open(LEDGER_PATH, encoding="utf-8"))
            except (ValueError, OSError):
                pass
        self.today = datetime.date.today().isoformat()
        self.used = 0

    def left(self) -> int:
        return self.cap - self.data.setdefault("days", {}).get(self.today, 0)

    def spend(self) -> None:
        if self.left() <= 0:
            raise SystemExit(f"daily anonymous cap reached ({self.cap}); stop for today")
        self.data["days"][self.today] = self.data["days"].get(self.today, 0) + 1
        self.used += 1
        self.save()

    def save(self) -> None:
        days = self.data.setdefault("days", {})
        cutoff = (datetime.date.today() - datetime.timedelta(days=KEEP_DAYS)).isoformat()
        self.data["days"] = {d: n for d, n in days.items() if d >= cutoff}
        tmp = LEDGER_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(self.data, handle, indent=1, sort_keys=True)
        os.replace(tmp, LEDGER_PATH)


# ------------------------------------------------------------------- fetch ---

def fetch(url: str, budget: Budget, pace: tuple[float, float], first: list[bool]) -> str:
    budget.spend()
    if not first[0]:
        time.sleep(random.uniform(*pace))
    first[0] = False
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept-Language": "uk-UA,uk;q=0.9,en;q=0.6",
    })
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 429):
            raise SystemExit(f"HTTP {exc.code} — Djinni is throttling; stop, do not retry in a loop")
        raise SystemExit(f"HTTP {exc.code} for {url}")
    except urllib.error.URLError as exc:
        raise SystemExit(f"network error for {url}: {exc.reason}")


def listing_url(query: str, page: int) -> str:
    params = [("all_keywords", query)]
    if page > 1:
        params.append(("page", str(page)))
    return f"{BASE}/jobs/?{urllib.parse.urlencode(params)}"


# -------------------------------------------------------------- seen store ---

def load_seen() -> dict:
    if not os.path.isfile(SEEN_PATH):
        return {}
    try:
        return json.load(open(SEEN_PATH, encoding="utf-8"))
    except (ValueError, OSError):
        return {}


def save_seen(seen: dict) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = SEEN_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(seen, handle, indent=1, sort_keys=True)
    os.replace(tmp, SEEN_PATH)


def seed_from_inbox(pages: int, seen: dict) -> int:
    """Mark vacancies I already applied to as seen (needs the account, a few requests)."""
    from mcp_client import session
    calls = [("call", {"name": "list_threads", "arguments": {"bucket": "apply", "page": p}})
             for p in range(1, max(1, pages) + 1)]
    ids = set()
    for reply in session(calls, read_only=True):
        ids.update(re.findall(r"/jobs/(\d+)-", json.dumps(reply, ensure_ascii=False)))
    today = datetime.date.today().isoformat()
    added = 0
    for job_id in ids:
        if job_id not in seen:
            seen[job_id] = {"first_seen": today, "posted_at": None, "title": None, "source": "inbox"}
            added += 1
    save_seen(seen)
    return added


# ---------------------------------------------------------------- classify ---

def classify(job: dict) -> str:
    published, updated = job.get("published_at"), job.get("updated_at")
    if not published:
        return "UNVERIFIED"
    if not updated or updated <= published:
        return "NEW"
    return "BUMPED"


def label(job: dict) -> str:
    kind = classify(job)
    if kind == "BUMPED":
        return f"BUMPED (pub {job['published_at']}, upd {job['updated_at']})"
    if kind == "NEW":
        return f"NEW (pub {job['published_at']})"
    return "NEW? (dates not read)"


def main() -> None:
    parser = argparse.ArgumentParser(description="Anonymous Djinni freshness scan")
    parser.add_argument("-q", "--query", default="DevOps", help="free-text search (all_keywords)")
    parser.add_argument("-p", "--pages", type=int, default=2, help="listing pages to read (default 2)")
    parser.add_argument("-c", "--check-limit", type=int, default=5,
                        help="how many unseen jobs to open for published/updated (default 5)")
    parser.add_argument("--no-check", action="store_true", help="do not open job pages at all")
    parser.add_argument("--cap", type=int, default=DEFAULT_CAP, help="anonymous requests per day")
    parser.add_argument("--pace", default="%.1f,%.1f" % DEFAULT_PACE,
                        help="seconds between requests, 'min,max'")
    parser.add_argument("--json", action="store_true", help="print JSON instead of a table")
    parser.add_argument("--reset-seen", action="store_true", help="forget the seen-store first")
    parser.add_argument("--seed-from-inbox", type=int, metavar="PAGES", default=0,
                        help="mark applied vacancies as seen (needs .env); N inbox pages")
    args = parser.parse_args()

    pace = tuple(float(x) for x in args.pace.split(",")) if "," in args.pace else (float(args.pace),) * 2
    first = [True]
    budget = Budget(args.cap)

    if args.reset_seen and os.path.isfile(SEEN_PATH):
        os.remove(SEEN_PATH)
        print(f"seen-store reset ({os.path.basename(SEEN_PATH)})")

    seen = load_seen()
    seeded = seed_from_inbox(args.seed_from_inbox, seen) if args.seed_from_inbox else 0
    initial = not seen

    collected, fetched_pages = {}, 0
    for page in range(1, max(1, args.pages) + 1):
        page_html = fetch(listing_url(args.query, page), budget, pace, first)
        fetched_pages += 1
        postings = _postings(page_html)
        if not postings and f"job-item-" in page_html:
            raise SystemExit("listing has job cards but no JSON-LD — markup changed, parsing needs an update")
        for job in postings:
            job["id"] = str(job.get("id") or "")
            if job["id"] and job["id"] not in collected:
                collected[job["id"]] = job

    unseen = [job for job_id, job in collected.items() if job_id not in seen]

    checked = 0
    if not args.no_check and unseen:
        for job in unseen[:max(0, args.check_limit)]:
            if budget.left() <= 0:
                print("(budget exhausted, remaining jobs left UNVERIFIED)", file=sys.stderr)
                break
            page_html = fetch(job["url"], budget, pace, first)
            published, updated = _publication(page_html)
            job["published_at"], job["updated_at"] = published, updated
            checked += 1

    order = {"NEW": 0, "UNVERIFIED": 1, "BUMPED": 2}
    unseen.sort(key=lambda j: (order.get(classify(j), 3), j.get("published_at") or j.get("posted_at") or "", j["id"]))

    today = datetime.date.today().isoformat()
    for job_id, job in collected.items():
        if job_id in seen:
            continue
        seen[job_id] = {"first_seen": today, "posted_at": job.get("posted_at"), "title": job.get("title")}
    save_seen(seen)

    if args.json:
        print(json.dumps({
            "date": today, "query": args.query, "pages": fetched_pages,
            "collected": len(collected), "unseen": len(unseen), "checked": checked,
            "budget_used": budget.used, "budget_left": budget.left(),
            "seeded_from_inbox": seeded, "initial_import": initial,
            "jobs": [{k: j.get(k) for k in ("id", "title", "company", "salary", "published_at",
                                            "updated_at", "posted_at", "url", "remote",
                                            "experience_years", "category")} | {"status": classify(j)}
                     for j in unseen],
        }, ensure_ascii=False, indent=1))
        return

    print(f"query: {args.query!r}  pages: {fetched_pages}  collected: {len(collected)}  "
          f"new to us: {len(unseen)}  dates checked: {checked}  budget: {budget.used} used, {budget.left()} left")
    if initial:
        print("initial import: everything on the pages is 'new to us' — the seen-store was empty")
    if seeded:
        print(f"seeded from inbox: {seeded} vacancy ids marked as already seen")
    if not unseen:
        print("\nNothing new: every vacancy on these pages was already in the seen-store (bumped ones keep their id).")
        return

    print()
    print("| # | status | title | company | link |")
    print("|---|---|---|---|---|")
    for index, job in enumerate(unseen, 1):
        title = (job.get("title") or "").replace("|", "/")[:60]
        company = (job.get("company") or "—").replace("|", "/")[:26]
        print(f"| {index} | {label(job)} | {title} | {company} | {job.get('url')} |")


if __name__ == "__main__":
    main()
