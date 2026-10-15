"""Slack leave workflow -> monthly Sheets calendar -> Teams daily digest."""
import calendar
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo


class DigestError(Exception):
    """Only fixed, non-sensitive error messages may reach public logs.

    kind selects the owners' alert wording: conflict, config, stale or delivery.
    detail names the offending record. It goes only to the owners' alert channel, never to logs.
    """

    def __init__(self, message, kind="config", detail=None):
        super().__init__(message)
        self.kind, self.detail = kind, detail


def required(key):
    value = os.getenv(key, "").strip()
    if not value:
        raise DigestError(f"Missing configuration: {key}")
    return value


def webhook_url(key):
    value = required(key)
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise DigestError(f"{key} must be an HTTPS webhook URL.")
    host = parsed.hostname.lower()
    if host == "outlook.office.com" or host.endswith(".webhook.office.com"):
        raise DigestError(f"{key} is a retired Office 365 connector URL; use a Teams Workflows webhook.")
    return value


def cell(row, col):
    return row[col] if col < len(row) else ""


def column(index):
    result = ""
    while index:
        index, rem = divmod(index - 1, 26)
        result = chr(65 + rem) + result
    return result


def sheet_ref(title):
    return "'" + title.replace("'", "''") + "'"


def normalized(value):
    return " ".join(str(value).casefold().split())


def employee_mapping(rows):
    if not rows or rows[0][:2] != ["Slack User ID", "Tracker Name"]:
        raise DigestError("Employee Mapping must have Slack User ID and Tracker Name headers in A1:B1.")
    if len(rows) >= 5000:
        raise DigestError("Employee Mapping reached its row limit.")
    mapping, names = {}, set()
    for row in rows[1:]:
        user, name = str(cell(row, 0)).strip(), str(cell(row, 1)).strip()
        if not user:
            continue  # Prefilled roster names can be mapped gradually.
        if not re.fullmatch(r"[UW][A-Z0-9]+", user) or not name or name.startswith("="):
            raise DigestError("Employee Mapping contains an invalid ID or missing/invalid tracker name.")
        if user in mapping or normalized(name) in names:
            raise DigestError("Employee Mapping contains duplicate IDs or tracker names.")
        mapping[user] = name
        names.add(normalized(name))
    return mapping


@dataclass(frozen=True)
class Leave:
    user: str
    start: date
    end: date
    code: str


REQUEST = re.compile(
    r"<@([UW][A-Z0-9]+)(?:\|[^>]+)?>\s+applied a\s+"
    r"Unplanned\s*[-–]\s*Sick/Emergency Leave\s+from\s+"
    r"(\d{4}-\d{2}-\d{2})\s+to\s+(\d{4}-\d{2}-\d{2})\.\s*"
    r"(Full Day|Half Day)(?=\s|$|[.,])", re.I,
)


RANGE = re.compile(r"\bfrom\s+(\d{4}-\d{2}-\d{2})\s+to\s+(\d{4}-\d{2}-\d{2})\b", re.I)


def ended(text, today):
    """True only if the text has exactly one 'from <date> to <date>' clause, both dates are
    valid, and both are before today. Any other date in the text is deliberately ignored, so a
    stray past date (e.g. 'applied on ...') can never hide a current leave."""
    ranges = RANGE.findall(text)
    if today is None or len(ranges) != 1:
        return False
    try:
        return max(date.fromisoformat(d) for d in ranges[0]) < today
    except ValueError:
        return False


def parse_request(message, today=None):
    # Workflow messages are bot-authored; the mentioned employee is the applicant.
    if not message.get("bot_id"):
        return None
    text = message.get("text", "").replace("*", "")
    ts = message.get("ts", "?")

    def reject(problem, detail):
        # A request we cannot read but whose dates are all in the past cannot affect the
        # calendar or the digest, so it must not block the run. Anything else fails closed.
        if ended(text, today):
            return None
        raise DigestError(f"{problem} (Slack message ts {ts}); inspect Slack privately.", "conflict", detail)

    match = REQUEST.search(text)
    if not match:
        if "unplanned" in text.casefold() and "applied a" in text.casefold():
            return reject("A leave workflow request has an unsupported format",
                          f"Slack message ts {ts}, user {message.get('user', '?')}")
        return None
    user, start, end, duration = match.groups()
    try:
        start, end = date.fromisoformat(start), date.fromisoformat(end)
    except ValueError:
        return reject("A Slack request contains an invalid date", f"Slack user {user}: {start} to {end}")
    if end < start or (end - start).days > 366:
        return reject("A Slack request has an invalid date range", f"Slack user {user}: {start} to {end}")
    return Leave(user, start, end, "SL" if duration.casefold() == "full day" else "HD")


class Month:
    def __init__(self, title, rows, year, month, evaluated=None):
        self.title, self.rows = title, rows
        if not rows:
            raise DigestError("A required monthly calendar is empty.")
        headers = [normalized(x) for x in rows[0]]
        if headers.count("name") != 1:
            raise DigestError("A calendar must have exactly one Name header in row 1.")
        self.name_col = headers.index("name")
        self.dates = {}
        for c, value in enumerate(rows[0]):
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                d = date(1899, 12, 30) + timedelta(days=int(value))
                if (d.year, d.month) == (year, month):
                    if d in self.dates:
                        raise DigestError("Duplicate calendar date headers.")
                    self.dates[d] = c
        if len(self.dates) != calendar.monthrange(year, month)[1]:
            raise DigestError("Calendar date headers are missing or not real spreadsheet dates.")
        self.people = {}
        for r, row in enumerate(rows[1:], 1):
            if str(cell(row, self.name_col)).startswith("="):
                if evaluated is None or r >= len(evaluated):
                    raise DigestError("Formula-derived names require evaluated spreadsheet values.")
                name_value = cell(evaluated[r], self.name_col)
                if (not isinstance(name_value, str) or not name_value.strip()
                        or name_value.startswith(("#", "=")) or name_value == "MISSING IN MASTER"):
                    raise DigestError("A formula-derived calendar name is blank or invalid; check the master mapping.")
                row[self.name_col] = name_value
            name = normalized(cell(row, self.name_col))
            if not name:
                continue
            if name in self.people:
                raise DigestError("Duplicate employee names in a monthly calendar.")
            if name.startswith("=") or any(str(cell(row, c)).startswith("=") for c in self.dates.values()):
                raise DigestError("Formula-driven leave cells are unsupported; use literal leave codes.")
            self.people[name] = r

    def code(self, row, day):
        return str(cell(self.rows[row], self.dates[day])).strip().upper()

    def plan(self, leave, name, today, last_day):
        start, end = max(leave.start, today), min(leave.end, last_day)
        # Weekend cells may be blank in the tracker; never invent leave on them.
        days = [d for d in self.dates if start <= d <= end and d.weekday() < 5]
        if not days:
            return []
        r = self.people.get(normalized(name))
        if r is None:
            raise DigestError("A mapped employee is missing from the calendar; check Employee Mapping privately.", "conflict",
                              f"Tracker name '{name}' not found in {self.title}")
        writes = []
        for day in days:
            old = self.code(r, day)
            # A full-day SL already covers a half-day request for the same day.
            if old in ("WK", "HL") or old == leave.code or (old == "SL" and leave.code == "HD"):
                continue
            if old:
                raise DigestError("Leave conflicts with an existing calendar entry; resolve it privately before retrying.",
                                  "conflict", f"{name}, {day:%d %b %Y}: tracker has {old}, Slack requested {leave.code}")
            c = self.dates[day]
            writes.append({"range": f"{sheet_ref(self.title)}!{column(c+1)}{r+1}", "values": [[leave.code]]})
            while len(self.rows[r]) <= c:
                self.rows[r].append("")
            self.rows[r][c] = leave.code
        return writes


def ordinal(n):
    suffix = "th" if 11 <= n % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def date_list(days):
    """Compress only genuinely consecutive dates ("19-30th Oct"); never invent leave on a holiday."""
    groups = []
    for d in sorted(set(days)):
        if groups and d == groups[-1][-1] + timedelta(days=1):
            groups[-1].append(d)
        else:
            groups.append([d])
    out = []
    for g in groups:
        a, b = g[0], g[-1]
        if a == b:
            out.append(f"{ordinal(a.day)} {a:%b}")
        elif (a.year, a.month) == (b.year, b.month):
            out.append(f"{ordinal(a.day)}-{ordinal(b.day)} {b:%b}")
        else:
            out.append(f"{ordinal(a.day)} {a:%b}-{ordinal(b.day)} {b:%b}")
    return ", ".join(out)


KNOWN_CODES = {"SL", "HD", "PL", "HL", "WK", "CPL"}
MAX_ITEMS = 50
MAX_CARD_BYTES = 24000  # Teams Workflows webhooks reject messages of roughly 28 KB and over.


def make_digest(months, today, last_day):
    sections = {"Upcoming Holidays": [], "Sick/Emergency Leaves": [], "Half-day Leaves": [],
                "Planned Leaves": [], "Upcoming Leaves": [], "Other Entries": []}
    holidays, planned = set(), {}
    for month in months:
        for name, r in month.people.items():
            display = str(cell(month.rows[r], month.name_col)).strip()
            for day in month.dates:
                code = month.code(r, day)
                if today <= day <= last_day and code == "HL":
                    holidays.add(day)
                if day == today:
                    if code == "SL":
                        sections["Sick/Emergency Leaves"].append(f"{display}: Sick/Emergency leave")
                    elif code == "HD":
                        # HD alone does not encode whether the leave was planned.
                        sections["Half-day Leaves"].append(f"{display}: Half day (type unspecified in tracker)")
                    elif code == "PL":
                        sections["Planned Leaves"].append(f"{display}: {ordinal(day.day)} {day:%b}")
                    elif code and code not in KNOWN_CODES:
                        # Never drop an entry leadership might need; show it unclassified.
                        sections["Other Entries"].append(f"{display}: {code} (unrecognized code)")
                if today < day <= last_day and code == "PL":
                    planned.setdefault(display, []).append(day)
    sections["Upcoming Holidays"] = [f"{d:%A, %B} {d.day}, {d.year}" for d in sorted(holidays)]
    sections["Upcoming Leaves"] = [f"{name}: {date_list(days)}" for name, days in sorted(planned.items())]
    lines = [f"Leaves: {ordinal(today.day)} {today:%B %Y}"]
    for title, items in sections.items():
        if title == "Other Entries" and not items:
            continue
        lines.extend(["", title + ":"])
        shown = [f"{i}. {item}" for i, item in enumerate(items[:MAX_ITEMS], 1)]
        if len(items) > MAX_ITEMS:
            shown.append(f"...and {len(items) - MAX_ITEMS} more")
        lines.extend(shown or ["None"])
    return "\n".join(lines)


class Sheets:
    def __init__(self):
        from google.oauth2 import service_account
        from google.auth.transport.requests import AuthorizedSession
        credentials = service_account.Credentials.from_service_account_info(
            json.loads(required("GOOGLE_CREDENTIALS_JSON")),
            scopes=["https://www.googleapis.com/auth/spreadsheets"])
        self.http = AuthorizedSession(credentials)
        self.base = "https://sheets.googleapis.com/v4/spreadsheets/" + quote(required("SPREADSHEET_ID"), safe="")
        self.titles = {s["properties"]["title"] for s in self.request("GET", "", params={"fields": "sheets.properties.title"})["sheets"]}

    def request(self, method, path, **kwargs):
        response = self.http.request(method, self.base + path, timeout=45, **kwargs)
        if not response.ok:
            raise DigestError("Google Sheets API failed; check sharing, credentials, and calendar configuration.")
        return response.json()

    def read(self, title, area, render="FORMULA"):
        if title not in self.titles:
            raise DigestError("A required sheet tab is missing.")
        return self.request("GET", "/values/" + quote(f"{sheet_ref(title)}!{area}", safe=""),
                            params={"valueRenderOption": render, "dateTimeRenderOption": "SERIAL_NUMBER"}).get("values", [])

    def write(self, writes):
        if writes:
            self.request("POST", "/values:batchUpdate", json={"valueInputOption": "RAW", "data": writes})

    def state(self, today):
        title = "Digest State"
        if title not in self.titles:
            return None, 2
        rows = self.read(title, "A1:B5000")
        if rows and rows[0] != ["Date", "Status"]:
            raise DigestError("Digest State tab has unexpected headers.")
        if len(rows) >= 5000:
            raise DigestError("Digest State is full; archive older records privately.")
        for r, row in enumerate(rows[1:], 2):
            if cell(row, 0) == today.isoformat():
                return cell(row, 1), r
        return None, max(2, len(rows) + 1)

    def claim(self, today, row, status):
        if "Digest State" not in self.titles:
            self.request("POST", ":batchUpdate", json={"requests": [{"addSheet": {"properties": {"title": "Digest State"}}}]})
            self.titles.add("Digest State")
        self.write([{"range": "'Digest State'!A1:B1", "values": [["Date", "Status"]]},
                    {"range": f"'Digest State'!A{row}:B{row}", "values": [[today.isoformat(), status]]}])


def slack_requests(today, last_day):
    import requests
    lookback = int(os.getenv("SLACK_LOOKBACK_DAYS", "90"))
    if not 1 <= lookback <= 366:
        raise DigestError("SLACK_LOOKBACK_DAYS must be between 1 and 366.")
    zone = ZoneInfo(os.getenv("TIMEZONE", "Asia/Kolkata"))
    oldest = datetime.combine(today - timedelta(days=lookback), datetime.min.time(), zone).timestamp()
    params = {"channel": required("SLACK_CHANNEL_ID"), "oldest": str(oldest), "limit": 100}
    results, ignored = [], 0
    for _ in range(100):
        response = requests.get("https://slack.com/api/conversations.history", params=params,
                                headers={"Authorization": "Bearer " + required("SLACK_BOT_TOKEN")}, timeout=45)
        if not response.ok:
            raise DigestError("Slack history request failed or was rate limited; retry later.")
        data = response.json()
        if not data.get("ok"):
            raise DigestError("Slack denied channel history access; check bot token, scopes, and membership.")
        for message in data.get("messages", []):
            source = os.getenv("SLACK_LEAVE_BOT_ID", "")
            if source and message.get("bot_id") != source:
                continue
            leave = parse_request(message, today)
            if leave is None and message.get("bot_id"):
                ignored += 1
            if leave and leave.end >= today and leave.start <= last_day:
                results.append(leave)
        cursor = data.get("response_metadata", {}).get("next_cursor", "")
        if not cursor:
            if data.get("has_more"):
                raise DigestError("Slack history pagination was incomplete; no changes were made.")
            if ignored:  # Count only; a reworded workflow template would otherwise be invisible.
                print(f"{ignored} bot messages in the lookback window were not recognised as leave requests.")
            return results
        params["cursor"] = cursor
    raise DigestError("Slack history exceeded the scan limit; narrow the lookback.")


def build_card(title, style, text, link=None):
    """Adaptive Card for a Teams Workflows webhook (same envelope as the Azure pipelines).

    Card text is markdown, where a single newline collapses to a space, so every
    line becomes its own paragraph and every blank-line-separated group its own block.
    """
    if style:
        body = [{"type": "Container", "style": style, "bleed": True, "items": [
            {"type": "TextBlock", "text": title, "weight": "Bolder", "size": "Medium", "wrap": True}]}]
    else:  # leadership digest: plain text, same look as the previously hand-posted message
        body = [{"type": "TextBlock", "text": title, "wrap": True}]
    for group in text.split("\n\n"):
        lines = [line for line in group.split("\n") if line.strip()]
        if lines:
            body.append({"type": "TextBlock", "text": "\n\n".join(lines), "wrap": True, "spacing": "Medium"})
    card = {"$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "type": "AdaptiveCard", "version": "1.4", "body": body}
    if link:
        card["actions"] = [{"type": "Action.OpenUrl", "title": "View run", "url": link}]
    return {"type": "message", "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive",
                                                "contentUrl": None, "content": card}]}


def digest_card(text):
    """Leadership card: shaded title band, then one bold heading (with a count) and a bullet list per section.

    Bullets rather than numbers: Teams clips the number of a two-digit ordered-list item.
    """
    title, _, rest = text.partition("\n")
    body = [{"type": "Container", "style": "emphasis", "bleed": True, "items": [
        {"type": "TextBlock", "text": title, "weight": "Bolder", "size": "Large", "wrap": True}]}]
    for group in rest.strip("\n").split("\n\n"):
        heading, _, items = group.partition("\n")
        lines = [line for line in items.split("\n") if line.strip()]
        entries = [re.sub(r"^\d+\.\s+", "", line) for line in lines if re.match(r"\d+\.\s", line)]
        notes = [line for line in lines if not re.match(r"\d+\.\s", line) and line != "None"]
        name = heading.rstrip(":")
        body.append({"type": "TextBlock", "text": f"{name} ({len(entries)})" if entries else name,
                     "weight": "Bolder", "size": "Medium", "wrap": True, "separator": True, "spacing": "Large"})
        if entries:
            body.append({"type": "TextBlock", "text": "\n".join(f"- {e}" for e in entries), "wrap": True, "spacing": "Small"})
        else:
            body.append({"type": "TextBlock", "text": "None", "isSubtle": True, "spacing": "Small"})
        for note in notes:
            body.append({"type": "TextBlock", "text": note, "isSubtle": True, "wrap": True, "spacing": "None"})
    card = {"$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "type": "AdaptiveCard",
            "version": "1.4", "body": body}
    return {"type": "message", "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive",
                                                "contentUrl": None, "content": card}]}


ALERT_NEXT = {
    "conflict": "Needs a manual data fix in Slack or the tracker; then re-run.",
    "config": "Fix the configuration or access problem; then re-run.",
    "stale": "The tracker changed during the run. Safe to re-run.",
    "delivery": "Delivery to leadership is uncertain. Check the leadership channel and Power Automate run history BEFORE clearing today's Digest State row.",
    "unexpected": "Unexpected failure. Check the Actions run.",
}


def send_alert(kind, message, detail=None):
    """Tell the owners' channel the digest was not sent. Never raises; never masks the real error."""
    if os.getenv("RUN_MODE") != "live":
        return
    try:
        import requests
        server, repo, run_id = (os.getenv(k, "") for k in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID"))
        link = f"{server}/{repo}/actions/runs/{run_id}" if server and repo and run_id else None
        title = "Leave digest delivery needs review" if kind == "delivery" else "Leave digest NOT sent"
        text = f"{message}\n\n{detail}\n\n{ALERT_NEXT[kind]}" if detail else f"{message}\n\n{ALERT_NEXT[kind]}"
        card = build_card(title, "attention", text, link)
        response = requests.post(webhook_url("ALERT_WEBHOOK_URL"), json=card, timeout=15, allow_redirects=False)
        if not 200 <= response.status_code < 300:
            raise ValueError
        if os.getenv("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a") as out:
                out.write("alerted=true\n")
    except Exception:
        print("Owner alert could not be delivered; check the Actions run.", file=sys.stderr)


def run():
    mode = os.getenv("RUN_MODE", "preview")
    if mode not in ("preview", "live"):
        raise DigestError("RUN_MODE must be preview or live.")
    today = datetime.now(ZoneInfo(os.getenv("TIMEZONE", "Asia/Kolkata"))).date()
    if os.getenv("SKIP_WEEKENDS", "true").lower() == "true" and today.weekday() >= 5:
        print("Skipped weekend.")
        return
    if mode == "live":
        # Fail before touching the sheet if the leadership/alert targets are unsafe or missing.
        webhook = webhook_url("TEAMS_WEBHOOK_URL")
        webhook_url("ALERT_WEBHOOK_URL")
        required("SLACK_LEAVE_BOT_ID")
    days = int(os.getenv("UPCOMING_DAYS", "0"))
    if not 0 <= days <= 90:
        raise DigestError("UPCOMING_DAYS must be 0 (month end) or 1–90.")
    last_day = today + timedelta(days=days) if days else today.replace(day=calendar.monthrange(today.year, today.month)[1])
    sheets = Sheets()
    mapping = employee_mapping(sheets.read("Employee Mapping", "A1:B5000"))
    months, originals, current = [], {}, today.replace(day=1)
    max_rows = int(os.getenv("CALENDAR_MAX_ROWS", "500"))
    if not 2 <= max_rows <= 5000:
        raise DigestError("CALENDAR_MAX_ROWS must be 2–5000.")
    while current <= last_day:
        title = current.strftime("%B %Y")
        rows = sheets.read(title, f"A1:AZ{max_rows}")
        originals[title] = json.loads(json.dumps(rows))
        if len(rows) >= max_rows:
            raise DigestError("Calendar row limit reached; increase CALENDAR_MAX_ROWS.")
        headers = [normalized(x) for x in rows[0]] if rows else []
        name_col = headers.index("name") if "name" in headers else None
        evaluated = None
        if name_col is not None and any(str(cell(row, name_col)).startswith("=") for row in rows[1:]):
            evaluated = sheets.read(title, f"A1:AZ{max_rows}", render="UNFORMATTED_VALUE")
        months.append(Month(title, rows, current.year, current.month, evaluated=evaluated))
        current = (current.replace(day=28) + timedelta(days=4)).replace(day=1)
    this_month = months[0]
    if os.getenv("SKIP_HOLIDAYS", "true").lower() == "true":
        codes = [this_month.code(r, today) for r in this_month.people.values()]
        if codes and all(c == "HL" for c in codes):
            print("Skipped company holiday.")
            return
    status, state_row = sheets.state(today)
    if mode == "live" and status == "accepted":
        print("Today's digest was already sent. No changes made.")
        return
    if mode == "live" and status and status != "failed":
        # pending, or a hand-typed value: delivery is unverified. Alert instead of exiting quietly.
        raise DigestError("Today's digest is awaiting delivery verification (state is not accepted or failed); nothing was sent.",
                          "delivery")
    writes = []
    leaves = slack_requests(today, last_day)
    # Full-day SL first, so a same-day half-day request is covered by it instead of conflicting.
    # The channel covers people outside this team: only mapped applicants are processed.
    ignored = {leave.user for leave in leaves if leave.user not in mapping}
    if ignored:
        print(f"{len(ignored)} Slack applicant(s) with active requests are not in Employee Mapping and were ignored.")
    leaves = [leave for leave in leaves if leave.user in mapping]
    for leave in sorted(leaves, key=lambda l: l.code != "SL"):
        for month in months:
            writes.extend(month.plan(leave, mapping[leave.user], today, last_day))
    text = make_digest(months, today, last_day)
    card = digest_card(text)
    if len(json.dumps(card)) > MAX_CARD_BYTES:
        raise DigestError("The digest card is too large for a Teams webhook; reduce the roster window.")
    if mode == "preview":
        # Never print HR data or upload it as a public Actions artifact.
        private_path = os.getenv("PRIVATE_PREVIEW_PATH", "")
        if private_path:
            if os.getenv("GITHUB_ACTIONS") == "true":
                raise DigestError("Private preview files are only supported outside GitHub Actions.")
            fd = os.open(private_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as out:
                out.write(text)
        print(f"Preview passed: {len(leaves)} active requests; {len(writes)} calendar updates. No writes or Teams post.")
        return
    import requests
    # Refetch before writes to detect intervening edits. Sheets has no conditional cell write.
    for month in months:
        if sheets.read(month.title, f"A1:AZ{max_rows}") != originals[month.title]:
            raise DigestError("Calendar changed during the run; retry after reviewing privately.", "stale")
    if employee_mapping(sheets.read("Employee Mapping", "A1:B5000")) != mapping:
        raise DigestError("Employee Mapping changed during the run; retry after reviewing privately.", "stale")
    sheets.write(writes)
    # Reserve before POST. An ambiguous timeout must not cause an automatic duplicate.
    sheets.claim(today, state_row, "pending")
    try:
        response = requests.post(webhook, json=card, timeout=45, allow_redirects=False)
        if response.status_code == 429 or response.status_code >= 500:
            time.sleep(5)  # one retry for a transient rejection; nothing was accepted, so no duplicate risk
            response = requests.post(webhook, json=card, timeout=45, allow_redirects=False)
    except requests.RequestException:
        # Ambiguous: the message may have arrived. State stays pending to block an automatic duplicate.
        raise DigestError("Teams delivery outcome is unknown; verify delivery privately before clearing pending state.",
                          "delivery") from None
    if not 200 <= response.status_code < 300:
        # Definite rejection: nothing was posted. 'failed' lets a manual same-day live re-run retry.
        try:
            sheets.claim(today, state_row, "failed")
        except DigestError:
            raise DigestError("Teams rejected the webhook request and the failed state could not be recorded; "
                              "state stays pending.", "delivery") from None
        raise DigestError("Teams rejected the webhook request; nothing was posted. Fix the webhook, then re-run today's live job manually.")
    try:
        sheets.claim(today, state_row, "accepted")
    except DigestError:
        raise DigestError("Digest was posted but its accepted state could not be recorded; state stays pending.",
                          "delivery") from None
    print("Calendar updated; Teams webhook accepted. Verify delivery in Teams/Power Automate run history.")


def main():
    try:
        run()
    except DigestError as exc:
        send_alert(exc.kind, str(exc), exc.detail)
        raise
    except Exception:
        send_alert("unexpected", "The digest job failed unexpectedly.")
        raise


if __name__ == "__main__":
    try:
        main()
    except DigestError as exc:
        print(str(exc), file=sys.stderr)
        if exc.detail and os.getenv("GITHUB_ACTIONS") != "true":
            print("Detail (local run only):", exc.detail, file=sys.stderr)  # never in public Actions logs
        sys.exit(1)
    except Exception:
        # Exception strings and tracebacks can contain webhook URLs, names, or API bodies.
        print("Run failed. Check credentials, configuration and private source data; no response bodies were logged.", file=sys.stderr)
        sys.exit(1)
