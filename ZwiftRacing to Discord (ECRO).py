import os
import re
import sys
import json
import time
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from playwright.sync_api import sync_playwright
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

# Windows consoles default to cp1252, which cannot print characters like
# the zero-width spaces zwiftracing.app embeds in page text — a debug print
# would crash the scrape. Force UTF-8 and replace anything unprintable.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# -----------------------------
# CONFIG
# -----------------------------
ZWR_BASE_URL  = "https://www.zwiftracing.app"
ZWR_EVENTS_URL  = f"{ZWR_BASE_URL}/events"
ZWR_RESULTS_URL = f"{ZWR_BASE_URL}/results"

EVENT_QUERY  = "ECRO"
TEAM_FILTER  = "usmes"   # case-insensitive match on team column
# Optional limit: scrape only the N most recent events (listing is sorted
# newest-first). Set the MAX_EVENTS environment variable; 0/unset = all.
MAX_EVENTS   = int(os.getenv("MAX_EVENTS", "0") or 0)

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
POSTED_FILE         = os.path.join(os.path.dirname(__file__), "posted_zwiftracing_ecro.txt")

# Google account used for the automated OAuth sign-in.
# GOOGLE_EMAIL picks the right account on Google's account-chooser screen.
# GOOGLE_PASSWORD is only used as a last resort when the Edge profile has no
# active Google session — Google often blocks password entry from automated
# browsers, so the reliable path is keeping the profile's Google session alive.
GOOGLE_EMAIL    = os.getenv("GOOGLE_EMAIL", "")
GOOGLE_PASSWORD = os.getenv("GOOGLE_PASSWORD", "")

LOCAL_TZ = ZoneInfo("Pacific/Honolulu")

CATEGORY_EMOJIS = {
    "A": "🟥",
    "B": "🟩",
    "C": "🟦",
    "D": "🟨",
    "E": "🟪",
}

# -----------------------------
# UTILITIES
# -----------------------------
def load_posted_ids():
    if not os.path.exists(POSTED_FILE):
        return set()
    with open(POSTED_FILE, "r", encoding="utf-8") as f:
        return set(line.strip() for line in f)

def save_posted_id(event_id):
    with open(POSTED_FILE, "a", encoding="utf-8") as f:
        f.write(f"{event_id}\n")

def wait_for_spa(page, timeout=8000):
    """Let the React/Vue bundle hydrate before inspecting the DOM."""
    try:
        page.wait_for_load_state("networkidle", timeout=timeout)
    except Exception:
        pass
    page.wait_for_timeout(1000)

# -----------------------------
# SESSION / LOGIN
# -----------------------------
# zwiftracing.app authenticates with NextAuth (Google or Strava OAuth).
# A persistent Edge profile keeps the session cookie between runs, so the
# manual login is only needed once (and again whenever the session expires).
PROFILE_DIR = os.path.join(os.path.dirname(__file__), "edge_profile")


def launch_browser(p):
    """
    Launch Edge with a persistent profile. Cookies, localStorage and
    IndexedDB survive restarts, so the NextAuth session is reused.
    No user-agent override: a spoofed UA contradicts Edge's sec-ch-ua
    client hints, which makes Google reject the OAuth login as an
    insecure browser.
    """
    context = p.chromium.launch_persistent_context(
        PROFILE_DIR,
        channel="msedge",   # use installed Microsoft Edge
        headless=False,     # Google blocks headless browsers outright
        args=["--disable-blink-features=AutomationControlled"],
        ignore_default_args=["--enable-automation"],
        timezone_id="Pacific/Honolulu",
        locale="en-US",
        viewport={"width": 1280, "height": 900},
    )
    page = context.pages[0] if context.pages else context.new_page()
    return context, page


def is_logged_in(page):
    """
    Ask NextAuth directly: /api/auth/session returns {} for anonymous
    visitors and a session object with a "user" key when authenticated.
    (The site serves /events and /results to anonymous users too, so
    URL/body checks cannot tell the two states apart.)
    """
    try:
        resp = page.request.get(f"{ZWR_BASE_URL}/api/auth/session")
        session = resp.json() if resp.ok else {}
        user = session.get("user") if isinstance(session, dict) else None
        if user:
            print(f"  Logged in as: {user.get('name') or user.get('email') or 'unknown user'}")
            return True
        return False
    except Exception as e:
        print(f"  Login check error: {e}")
        return False


def _drive_google_oauth(page, timeout_s=90):
    """
    Walk the accounts.google.com flow until we're redirected back to
    zwiftracing.app. Handles, in order of preference:
      - account chooser (profile already signed into Google → just click)
      - email + password entry from .env (best effort — Google may block)
      - 2-step verification (waits for the user to approve on their phone)
      - consent / "Continue" screens
    Returns True once we're back on the site, False on block or timeout.
    """
    deadline = time.time() + timeout_s
    warned_2fa = False

    while time.time() < deadline:
        url = page.url

        # Back on zwiftracing.app (past the /api/auth callback) → done
        if url.startswith(ZWR_BASE_URL) and "/api/auth" not in url:
            return True

        if "accounts.google.com" in url:
            try:
                body = page.inner_text("body").lower()
            except Exception:
                body = ""

            # Hard block: Google refused the automated browser
            if "may not be secure" in body or "couldn't sign you in" in body:
                print("  Google blocked the automated sign-in ('browser may not be secure').")
                return False

            # Account chooser — the profile already has a Google session
            try:
                target = None
                if GOOGLE_EMAIL:
                    target = page.query_selector(f"[data-identifier='{GOOGLE_EMAIL}' i]")
                if not target:
                    target = page.query_selector("[data-identifier]")
                if target and target.is_visible():
                    print(f"  Account chooser → selecting {target.get_attribute('data-identifier')}")
                    target.click()
                    page.wait_for_timeout(2500)
                    continue
            except Exception:
                pass

            # Email entry
            try:
                email_in = page.query_selector("input[type='email']")
                if email_in and email_in.is_visible():
                    if not GOOGLE_EMAIL:
                        print("  Google wants an email but GOOGLE_EMAIL is not set in .env.")
                        return False
                    print("  Entering Google email…")
                    email_in.fill(GOOGLE_EMAIL)
                    page.keyboard.press("Enter")
                    page.wait_for_timeout(3000)
                    continue
            except Exception:
                pass

            # Password entry
            try:
                pw_in = page.query_selector("input[type='password']")
                if pw_in and pw_in.is_visible():
                    if not GOOGLE_PASSWORD:
                        print("  Google wants the password but GOOGLE_PASSWORD is not set in .env.")
                        return False
                    print("  Entering Google password…")
                    pw_in.fill(GOOGLE_PASSWORD)
                    page.keyboard.press("Enter")
                    page.wait_for_timeout(3500)
                    continue
            except Exception:
                pass

            # 2-step verification — needs the human's phone, so just wait
            if not warned_2fa and ("2-step verification" in body or "verify it" in body):
                print("  Google is asking for 2-step verification — approve it on your phone; waiting…")
                warned_2fa = True

            # Consent / continue screens
            for sel in ["button:has-text('Continue')", "button:has-text('Allow')", "#submit_approve_access"]:
                try:
                    el = page.query_selector(sel)
                    if el and el.is_visible():
                        el.click()
                        page.wait_for_timeout(2500)
                        break
                except Exception:
                    pass

        page.wait_for_timeout(1000)

    print("  Timed out waiting for the Google OAuth flow to complete.")
    return False


def automated_google_login(page):
    """
    Start the NextAuth Google sign-in and drive it to completion.
    Reliable when the Edge profile already holds a Google session
    (only the account chooser needs clicking — no password, so no
    bot-detection surface). Falls back to typing .env credentials.
    """
    print("  Attempting automated Google sign-in…")
    try:
        page.goto(f"{ZWR_BASE_URL}/api/auth/signin", timeout=30000)
        wait_for_spa(page)

        clicked = False
        for sel in [
            "form[action*='signin/google'] button",
            "button:has-text('Sign in with Google')",
            "button:has-text('Google')",
            "a:has-text('Sign in with Google')",
        ]:
            try:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    el.click()
                    clicked = True
                    break
            except Exception:
                pass

        if not clicked:
            print("  Could not find the 'Sign in with Google' button on the signin page.")
            return False

        page.wait_for_timeout(2500)
        if not _drive_google_oauth(page):
            return False

        wait_for_spa(page)
        return is_logged_in(page)

    except Exception as e:
        print(f"  Automated Google sign-in error: {e}")
        return False


def ensure_logged_in(page, max_attempts=3):
    """
    Verify the session; if anonymous, first try the automated Google
    sign-in, then fall back to a manual login in the open Edge window.
    """
    page.goto(ZWR_EVENTS_URL, timeout=30000)
    wait_for_spa(page)

    if is_logged_in(page):
        return True

    if automated_google_login(page):
        print("  Automated Google sign-in succeeded.")
        return True

    print("\n" + "=" * 60)
    print("ACTION REQUIRED — one-time manual login needed.")
    print("In the Edge window that just opened, please:")
    print("  1. Click LOGIN on zwiftracing.app")
    print("  2. Sign in with Google (or Strava)")
    print("     (if Google says 'browser or app may not be secure', use Strava)")
    print("  3. Wait until the site shows you as logged in")
    print("  4. Come back here and press ENTER")
    print("After this, your Google session lives in the Edge profile and")
    print("future re-logins happen automatically — no typing needed.")
    print("=" * 60 + "\n")

    for attempt in range(1, max_attempts + 1):
        input("Press ENTER after you are fully logged in… ")
        if is_logged_in(page):
            print("  Login confirmed — session stored in the Edge profile for future runs.")
            return True
        print(f"  Still not logged in (attempt {attempt}/{max_attempts}) — finish the login in the Edge window, then press ENTER again.")

    return False

# -----------------------------
# FIND ECRO EVENT IDs
# -----------------------------
def find_ecro_event_ids(page):
    """
    Scan the events and results pages on zwiftracing.app and collect
    numeric event IDs whose title contains EVENT_QUERY.
    """
    seen = set()
    event_ids = []

    for scan_url in [ZWR_RESULTS_URL]:
        print(f"\nScanning {scan_url} for {EVENT_QUERY} events…")
        try:
            # Skip navigation if already on this URL (fetch_events pre-loads results page)
            if not page.url.startswith(scan_url):
                page.goto(scan_url, timeout=30000)
                wait_for_spa(page)

            # Debug: show page title and visible text snippet
            print(f"  Page title: {page.title()}")
            body_text = page.inner_text("body")
            print(f"  Body snippet (first 400 chars):\n  {body_text[:400].replace(chr(10), ' ')}")

            # Debug: show all /events/ links regardless of text
            all_event_links = page.query_selector_all("a[href*='/events/']")
            print(f"  Total /events/ links found: {len(all_event_links)}")
            for link in all_event_links[:10]:
                try:
                    print(f"    href={link.get_attribute('href')!r}  text={link.inner_text().strip()!r}")
                except Exception:
                    pass

            # Filter the table by event title. The results page has a
            # "Title" filter (#title-search) that only applies on Enter,
            # and needs real keystrokes for its React onChange handler.
            for sel in [
                "#title-search",
                "input[placeholder*='search' i]",
                "input[type='search']",
                "input[placeholder*='filter' i]",
                "input[placeholder*='event' i]",
            ]:
                try:
                    el = page.query_selector(sel)
                    if el and el.is_visible():
                        el.click()
                        el.fill("")
                        el.type(EVENT_QUERY, delay=100)
                        el.press("Enter")
                        page.wait_for_timeout(3000)
                        print(f"  Filtered via {sel}")
                        break
                except Exception:
                    pass

            # Harvest every link that points to /events/<digits>
            links = page.query_selector_all("a[href*='/events/']")
            for link in links:
                try:
                    href = link.get_attribute("href") or ""
                    text = link.inner_text().strip()
                    m = re.search(r"/events/(\d+)", href)
                    if m and EVENT_QUERY.upper() in text.upper():
                        eid = m.group(1)
                        if eid not in seen:
                            seen.add(eid)
                            event_ids.append((eid, text))
                            print(f"  + {text}  (ID {eid})")
                except Exception:
                    pass

            # Page through if pagination exists
            while True:
                next_btn = page.query_selector(
                    "a:has-text('Next'):not([aria-disabled='true']), "
                    "button:has-text('Next'):not(:disabled)"
                )
                if not next_btn:
                    break
                next_btn.click()
                page.wait_for_timeout(1500)
                for link in page.query_selector_all("a[href*='/events/']"):
                    try:
                        href = link.get_attribute("href") or ""
                        text = link.inner_text().strip()
                        m = re.search(r"/events/(\d+)", href)
                        if m and EVENT_QUERY.upper() in text.upper():
                            eid = m.group(1)
                            if eid not in seen:
                                seen.add(eid)
                                event_ids.append((eid, text))
                                print(f"  + {text}  (ID {eid})")
                    except Exception:
                        pass

        except Exception as e:
            print(f"  Error scanning {scan_url}: {e}")

    return event_ids   # list of (id_str, title_hint)

# -----------------------------
# RESULTS TABLE PARSER
# -----------------------------
_TIME_RE  = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?(\.\d+)?$")
_WKG_RE   = re.compile(r"^\d+\.\d+$")
_RANK_RE  = re.compile(r"^\d+$")

def _text(el):
    try:
        return el.inner_text().strip()
    except Exception:
        return ""

def parse_results_table(page):
    """
    Extract a list of rider result dicts from the event page.
    zwiftracing.app renders a JS table; we try several selector strategies.
    """
    results = []

    # Strategy 1: standard <table> rows
    rows = page.query_selector_all("table tbody tr")

    # Strategy 2: div-based virtual table (common in React lists)
    if not rows:
        rows = page.query_selector_all(
            "[class*='result-row'], [class*='ResultRow'], "
            "[class*='raceResult'], [class*='rider-row']"
        )

    # Strategy 3: any <tr> that has 3+ <td> children
    if not rows:
        rows = page.query_selector_all("tr")

    if not rows:
        snippet = page.inner_text("body")[:600].replace("\n", " ")
        print(f"  No results table detected. Page snippet:\n  {snippet}")
        return results

    print(f"  Processing {len(rows)} row(s)…")

    for row in rows:
        try:
            cells = row.query_selector_all("td")
            if len(cells) < 3:
                cells = row.query_selector_all("[class*='cell'], [class*='col']")
            if len(cells) < 3:
                continue

            texts = [_text(c) for c in cells]
            # Cells pack extra data on following lines (vELO rating, time
            # gap) — the first line is the value the column displays.
            lines0 = [t.split("\n")[0].strip() for t in texts]

            # --- rank: first all-digit cell after cell 0 (cell 0 holds a
            # vELO ranking value, the Result column comes later) ---
            rank = next((t for t in lines0[1:] if t and _RANK_RE.match(t)), "")

            # --- rider name (prefer a link; remember which cell held it) ---
            rider = ""
            rider_idx = None
            for i, cell in enumerate(cells):
                el = cell.query_selector(
                    "a[href*='/riders/'], a[href*='/rider/'], "
                    "a[href*='/athletes/'], a[href*='/profile/']"
                )
                if el:
                    rider = _text(el).split("\n")[0].strip()
                    rider_idx = i
                    break
            if not rider:
                # Fall back: first non-numeric, non-empty cell text of reasonable length
                for t in lines0[1:]:
                    if t and not _RANK_RE.match(t) and len(t) > 2:
                        rider = t
                        break

            # --- team: the column right after the rider name cell ---
            team = ""
            if rider_idx is not None and rider_idx + 1 < len(lines0):
                candidate = lines0[rider_idx + 1]
                if candidate and not _TIME_RE.match(candidate):
                    team = candidate
            if not team:
                for sel in [
                    "a[href*='/teams/']",
                    "[class*='team']",
                    "[class*='Team']",
                ]:
                    el = row.query_selector(sel)
                    if el:
                        candidate = _text(el)
                        # Avoid picking up the rider link accidentally
                        if candidate and candidate != rider:
                            team = candidate
                            break

            # --- category ---
            race_cat = ""
            for sel in [
                "[class*='category']",
                "[class*='cat-badge']",
                "[class*='catBadge']",
                "span[class*='badge']",
                "[class*='Category']",
            ]:
                el = row.query_selector(sel)
                if el:
                    t = _text(el).upper()
                    if len(t) == 1 and t in "ABCDE":
                        race_cat = t
                        break
            # Also check raw cell texts for a single A-E letter
            if not race_cat:
                for t in lines0:
                    if len(t) == 1 and t.upper() in "ABCDE":
                        race_cat = t.upper()
                        break

            # --- time + gap to winner (second line of the time cell) ---
            race_time = next((t for t in lines0 if _TIME_RE.match(t)), "")
            gap = ""
            if race_time:
                idx = lines0.index(race_time)
                for ln in texts[idx].split("\n"):
                    ln = ln.strip()
                    if ln.startswith("+"):
                        gap = re.sub(r"\.\d+$", "", ln)
                        break
                race_time = re.sub(r"\.\d+$", "", race_time)

            # --- avg w/kg ---
            avg_wkg = next((t for t in lines0 if _WKG_RE.match(t)), "")

            # --- watts ---
            watts = ""
            for t in texts:
                m = re.match(r"^(\d+)\s*[Ww]$", t)
                if m:
                    watts = m.group(1)
                    break

            # --- HR ---
            avg_hr = max_hr = ""
            for t in texts:
                m = re.match(r"^(\d{2,3})\s*/\s*(\d{2,3})$", t)
                if m:
                    avg_hr, max_hr = m.group(1), m.group(2)
                    break

            if not rider:
                continue

            results.append({
                "rank":     rank,
                "rider":    rider,
                "team":     team,
                "race_cat": race_cat,
                "time":     race_time,
                "gap":      gap,
                "avg_wkg":  avg_wkg,
                "watts":    watts,
                "avg_hr":   avg_hr,
                "max_hr":   max_hr,
            })

        except Exception as e:
            print(f"  Row parse error: {e}")

    return results

# -----------------------------
# SCRAPE ONE EVENT
# -----------------------------
def scrape_event(page, event_id, title_hint=""):
    url = f"{ZWR_BASE_URL}/events/{event_id}"
    print(f"\nScraping event {event_id}: {url}")

    try:
        page.goto(url, timeout=30000)
        wait_for_spa(page)
    except Exception as e:
        print(f"  Navigation error: {e}")
        return None

    # --- title ---
    title = title_hint or f"ECRO Event {event_id}"
    for sel in ["h1", "h2", "h3", "[class*='eventTitle']", "[class*='event-title']", "[class*='EventName']"]:
        el = page.query_selector(sel)
        if el:
            t = _text(el)
            if t:
                title = t
                break

    body_text = page.inner_text("body")

    # --- date/time → Discord timestamp ---
    # The page has no machine-readable date element; parse the visible
    # "June 19, 2026 11:30 PM" text. The browser context is pinned to
    # LOCAL_TZ, so that's the timezone the page renders in.
    discord_ts = ""
    m = re.search(
        r"(January|February|March|April|May|June|July|August|September|"
        r"October|November|December)\s+\d{1,2},\s+\d{4}\s+\d{1,2}:\d{2}\s+[AP]M",
        body_text,
    )
    if m:
        raw = re.sub(r"\s+", " ", m.group(0))
        try:
            dt = datetime.strptime(raw, "%B %d, %Y %I:%M %p").replace(tzinfo=LOCAL_TZ)
            discord_ts = f"<t:{int(dt.timestamp())}:F>"
        except Exception:
            discord_ts = raw

    # --- route/course: values sit just above their ROUTE/DISTANCE/
    # ELEVATION labels in the page text ---
    course = "Unknown"
    body_lines = [ln.strip() for ln in body_text.split("\n")]

    def _labeled(label):
        try:
            i = body_lines.index(label)
        except ValueError:
            return ""
        for j in range(i - 1, max(i - 4, -1), -1):
            if body_lines[j]:
                return body_lines[j]
        return ""

    route = _labeled("ROUTE")
    if route:
        parts = [route, _labeled("DISTANCE"), _labeled("ELEVATION")]
        course = "   ".join(p for p in parts if p)

    # --- click Results tab if present ---
    for sel in [
        "button:has-text('Results')",
        "a:has-text('Results')",
        "[role='tab']:has-text('Results')",
        "li:has-text('Results')",
    ]:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                el.click()
                page.wait_for_timeout(2000)
                break
        except Exception:
            pass

    # --- scrape all pens: the event page shows pens A-E as tabs and only
    # loads pen A by default; click through each tab and tag riders with
    # its letter ---
    all_results = []
    seen_riders = set()
    pen_tabs = []
    for t in page.query_selector_all("[role='tab']"):
        first = _text(t)[:1].upper()
        if first and first in "ABCDE":
            pen_tabs.append((t, first))

    if pen_tabs:
        for tab, cat in pen_tabs:
            try:
                tab.click()
                page.wait_for_timeout(1200)
            except Exception as e:
                print(f"  Pen {cat} tab click failed: {e}")
                continue
            pen_results = parse_results_table(page)
            added = 0
            for r in pen_results:
                if r["rider"] in seen_riders:
                    continue
                seen_riders.add(r["rider"])
                if not r["race_cat"]:
                    r["race_cat"] = cat
                all_results.append(r)
                added += 1
            print(f"  Pen {cat}: {added} rider(s)")
    else:
        all_results = parse_results_table(page)

    usmes = [r for r in all_results if TEAM_FILTER in r["team"].lower()]

    print(f"  Total riders scraped: {len(all_results)} | USMeS: {len(usmes)}")

    return {
        "event_id":    event_id,
        "title":       title,
        "discord_ts":  discord_ts,
        "course":      course,
        "result_link": url,
        "finishers":   usmes,
    }

# -----------------------------
# DISCORD MESSAGE
# -----------------------------
def build_discord_message(event):
    """
    Two lines per rider, e.g.:
      Rank 2 🥈 🟨 [D] Em Kullman USMeS (USMeS) — 2:02:25
      +01:51 — 3.2 w/kg — HR 169bpm/182bpm
    (watts/HR/flags are ZwiftPower fields zwiftracing.app doesn't expose;
    segments are simply omitted when the data isn't there.)
    """
    header = [event["title"]]
    if event["discord_ts"]:
        header.append(f"📅 {event['discord_ts']} (adjusts to your timezone)")
    if event["course"] != "Unknown":
        header.append(f"🗺️ {event['course']}")
    header.append(f"🔗 {event['result_link']}")

    lines = []
    for f in event["finishers"]:
        rank  = f["rank"]
        medal = {"1": " 🥇", "2": " 🥈", "3": " 🥉"}.get(rank, "")
        rank_str = f"Rank {rank}" if rank else "Rank ?"

        cat       = f["race_cat"] or "?"
        cat_emoji = CATEGORY_EMOJIS.get(cat, "⬜")
        team_str  = f" ({f['team']})" if f["team"] else ""
        time_str  = f" — {f['time']}" if f["time"] else ""
        # Names with * or _ would render as Discord markdown italics
        rider = f["rider"].replace("*", "").replace("_", " ").strip()

        lines.append(
            f"{rank_str}{medal} {cat_emoji} [{cat}] "
            f"{rider}{team_str}{time_str}"
        )

        detail = []
        if f["gap"]:
            detail.append(f["gap"])
        wkg = f["avg_wkg"]
        if wkg:
            try:
                wkg = f"{float(wkg):.1f}"
            except ValueError:
                pass
        if f["watts"] and wkg:
            detail.append(f"{f['watts']}W ({wkg} w/kg)")
        elif f["watts"]:
            detail.append(f"{f['watts']}W")
        elif wkg:
            detail.append(f"{wkg} w/kg")
        if f["avg_hr"] and f["max_hr"]:
            detail.append(f"HR {f['avg_hr']}bpm/{f['max_hr']}bpm")
        elif f["avg_hr"]:
            detail.append(f"HR {f['avg_hr']}bpm")
        if detail:
            lines.append(" — ".join(detail))

    body = "\n".join(lines) if lines else "No USMeS riders found."

    return "\n".join(header) + "\n\n" + body[:1800]

# -----------------------------
# MAIN SCRAPER
# -----------------------------
def fetch_events():
    print(f"Launching Edge to scrape zwiftracing.app for '{EVENT_QUERY}' events…")

    with sync_playwright() as p:
        context, page = launch_browser(p)

        if not ensure_logged_in(page):
            print("Could not establish a logged-in session — aborting scrape.")
            context.close()
            return []

        print("  Session active — starting scrape.")

        # Navigate to the results page for scanning
        page.goto(ZWR_RESULTS_URL, timeout=30000)
        wait_for_spa(page)

        # Collect ECRO event IDs from the listing pages
        id_hints = find_ecro_event_ids(page)

        if not id_hints:
            print("No ECRO events found. Check that the events are listed on zwiftracing.app/events or /results.")
            context.close()
            return []

        print(f"\nTotal ECRO events found: {len(id_hints)}")

        if MAX_EVENTS and len(id_hints) > MAX_EVENTS:
            print(f"  Limiting to the {MAX_EVENTS} most recent events.")
            id_hints = id_hints[:MAX_EVENTS]

        # Don't re-scrape events that were already posted to Discord —
        # with 5 pens per event this saves a lot of time on repeat runs.
        posted_ids = load_posted_ids()

        events = []
        for event_id, title_hint in id_hints:
            if event_id in posted_ids:
                print(f"  Skipping already-posted event {event_id}")
                continue
            event = scrape_event(page, event_id, title_hint)
            if event:
                events.append(event)

        context.close()
        return events

# -----------------------------
# DISCORD POSTING
# -----------------------------
def send_events_to_discord(events):
    if not DISCORD_WEBHOOK_URL:
        print("DISCORD_WEBHOOK_URL is not set in .env — skipping Discord posting.")
        return

    posted_ids = load_posted_ids()

    for event in events:
        eid = event["event_id"]

        if eid in posted_ids:
            print(f"Skipping already-posted event {eid}")
            continue

        if not event["finishers"]:
            print(f"No USMeS riders in event {eid} — skipping Discord post.")
            continue

        content = build_discord_message(event)
        payload = {"content": content}

        resp = requests.post(
            DISCORD_WEBHOOK_URL,
            data=json.dumps(payload),
            headers={"Content-Type": "application/json"},
            timeout=30,
        )

        if resp.status_code in (200, 204):
            print(f"Posted event {eid} to Discord.")
            save_posted_id(eid)
        else:
            print(f"Failed to post event {eid}: HTTP {resp.status_code} — {resp.text}")

# -----------------------------
# ENTRY POINT
# -----------------------------
def main():
    events = fetch_events()
    print(f"\nScrape complete — {len(events)} ECRO event(s) processed.")
    send_events_to_discord(events)
    print("Done.")

if __name__ == "__main__":
    main()
