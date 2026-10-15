"""Slack leave workflow -> monthly Sheets calendar -> Teams daily digest."""
import calendar
import html
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo


class DigestError(Exception):
    """Only fixed, non-sensitive error messages may reach public logs."""


def required(key):
    value = os.getenv(key, "").strip()
    if not value:
        raise DigestError(f"Missing configuration: {key}")
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


def parse_request(message):
    # Workflow messages are bot-authored; the mentioned employee is the applicant.
    if not message.get("bot_id"):
        return None
    text = message.get("text", "").replace("*", "")
    match = REQUEST.search(text)
    if not match:
        if "unplanned" in text.casefold() and "applied a" in text.casefold():
            raise DigestError("A leave workflow request has an unsupported format; inspect Slack privately.")
        return None
    user, start, end, duration = match.groups()
    try:
        start, end = date.fromisoformat(start), date.fromisoformat(end)
    except ValueError:
        raise DigestError("A Slack request contains an invalid date.") from None
    if end < start or (end - start).days > 366:
        raise DigestError("A Slack request has an invalid date range.")
    return Leave(user, start, end, "SL" if duration.casefold() == "full day" else "HD")


class Month:
    def __init__(self, title, rows, year, month):
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
            name = normalized(cell(row, self.name_col))
            if not name:
                continue
            if name in self.people:
                raise DigestError("Duplicate employee names in a monthly calendar.")
            if name.startswith("=") or any(str(cell(row, c)).startswith("=") for c in self.dates.values()):
                raise DigestError("Formula-driven roster or calendar cells are unsupported; use literal values.")
            self.people[name] = r

    def code(self, row, day):
        return str(cell(self.rows[row], self.dates[day])).strip().upper()

    def plan(self, leave, name, today, last_day):
        start, end = max(leave.start, today), min(leave.end, last_day)
        days = [d for d in self.dates if start <= d <= end]
        if not days:
            return []
        r = self.people.get(normalized(name))
        if r is None:
            raise DigestError("A mapped employee is missing from the calendar; check Employee Mapping privately.")
        writes = []
        for day in days:
            old = self.code(r, day)
            if old in ("WK", "HL") or old == leave.code:
                continue
            if old:
                raise DigestError("Leave conflicts with an existing calendar entry; resolve it privately before retrying.")
            c = self.dates[day]
            writes.append({"range": f"{sheet_ref(self.title)}!{column(c+1)}{r+1}", "values": [[leave.code]]})
            while len(self.rows[r]) <= c:
                self.rows[r].append("")
            self.rows[r][c] = leave.code
        return writes


def date_list(days):
    """Compress only genuinely consecutive dates; never invent leave on a holiday."""
    groups = []
    for d in sorted(set(days)):
        if groups and d == groups[-1][-1] + timedelta(days=1):
            groups[-1].append(d)
        else:
            groups.append([d])
    return ", ".join(g[0].strftime("%d %b") if len(g) == 1 else
                     f"{g[0]:%d %b}–{g[-1]:%d %b}" for g in groups)


def make_digest(months, today, last_day):
    sections = {"Upcoming Holidays": [], "Sick/Emergency Leaves": [], "Half-day Leaves": [],
                "Planned Leaves": [], "Upcoming Leaves": []}
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
                        sections["Planned Leaves"].append(f"{display}: {day:%d %b}")
                if today < day <= last_day and code == "PL":
                    planned.setdefault(display, []).append(day)
    sections["Upcoming Holidays"] = [d.strftime("%A, %d %B %Y") for d in sorted(holidays)]
    sections["Upcoming Leaves"] = [f"{name}: {date_list(days)}" for name, days in sorted(planned.items())]
    lines = [f"Leaves: {today:%d %B %Y}"]
    for title, items in sections.items():
        lines.extend(["", title + ":"])
        lines.extend([f"{i}. {item}" for i, item in enumerate(items, 1)] or ["None"])
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

    def read(self, title, area):
        if title not in self.titles:
            raise DigestError("A required sheet tab is missing.")
        return self.request("GET", "/values/" + quote(f"{sheet_ref(title)}!{area}", safe=""),
                            params={"valueRenderOption": "FORMULA", "dateTimeRenderOption": "SERIAL_NUMBER"}).get("values", [])

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

    def create_mapping(self, month):
        title = "Employee Mapping"
        if title in self.titles:
            raise DigestError("Employee Mapping already exists; it was not overwritten.")
        result = self.request("POST", ":batchUpdate", json={"requests": [{"addSheet": {"properties": {
            "title": title, "gridProperties": {"rowCount": 5000, "columnCount": 2, "frozenRowCount": 1}}}}]})
        self.titles.add(title)
        rows = [["Slack User ID", "Tracker Name"]] + [["", str(cell(month.rows[r], month.name_col)).strip()]
                                                       for r in month.people.values()]
        self.write([{"range": "'Employee Mapping'!A1", "values": rows}])
        sheet_id = result["replies"][0]["addSheet"]["properties"]["sheetId"]
        self.request("POST", ":batchUpdate", json={"requests": [
            {"repeatCell": {"range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1},
                            "cell": {"userEnteredFormat": {"textFormat": {"bold": True},
                                     "backgroundColor": {"red": 0.85, "green": 0.91, "blue": 0.97}}},
                            "fields": "userEnteredFormat"}},
            {"updateDimensionProperties": {"range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 2},
                                           "properties": {"pixelSize": 300}, "fields": "pixelSize"}}]})


def slack_requests(today, last_day):
    import requests
    lookback = int(os.getenv("SLACK_LOOKBACK_DAYS", "90"))
    if not 1 <= lookback <= 366:
        raise DigestError("SLACK_LOOKBACK_DAYS must be between 1 and 366.")
    zone = ZoneInfo(os.getenv("TIMEZONE", "Asia/Kolkata"))
    oldest = datetime.combine(today - timedelta(days=lookback), datetime.min.time(), zone).timestamp()
    params = {"channel": required("SLACK_CHANNEL_ID"), "oldest": str(oldest), "limit": 100}
    results = []
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
            leave = parse_request(message)
            if leave and leave.end >= today and leave.start <= last_day:
                results.append(leave)
        cursor = data.get("response_metadata", {}).get("next_cursor", "")
        if not cursor:
            if data.get("has_more"):
                raise DigestError("Slack history pagination was incomplete; no changes were made.")
            return results
        params["cursor"] = cursor
    raise DigestError("Slack history exceeded the scan limit; narrow the lookback.")


def teams_payload(text):
    # The standard Teams Workflows incoming-webhook template accepts Adaptive Cards.
    return {"type": "message", "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive",
            "contentUrl": None, "content": {"$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "type": "AdaptiveCard", "version": "1.2", "body": [
                {"type": "TextBlock", "text": html.escape(text), "wrap": True}]}}]}


def main():
    mode = os.getenv("RUN_MODE", "preview")
    if mode not in ("preview", "live", "setup-mapping"):
        raise DigestError("RUN_MODE must be preview, live or setup-mapping.")
    today = datetime.now(ZoneInfo(os.getenv("TIMEZONE", "Asia/Kolkata"))).date()
    if mode == "setup-mapping":
        sheets = Sheets()
        title = today.strftime("%B %Y")
        max_rows = int(os.getenv("CALENDAR_MAX_ROWS", "500"))
        if not 2 <= max_rows <= 5000:
            raise DigestError("CALENDAR_MAX_ROWS must be 2–5000.")
        rows = sheets.read(title, f"A1:AZ{max_rows}")
        if len(rows) >= max_rows:
            raise DigestError("Calendar row limit reached.")
        sheets.create_mapping(Month(title, rows, today.year, today.month))
        employee_mapping(sheets.read("Employee Mapping", "A1:B5000"))
        print("Employee Mapping created with roster names. Fill Slack User IDs in the private tracker.")
        return
    if os.getenv("SKIP_WEEKENDS", "true").lower() == "true" and today.weekday() >= 5:
        print("Skipped weekend.")
        return
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
        months.append(Month(title, rows, current.year, current.month))
        current = (current.replace(day=28) + timedelta(days=4)).replace(day=1)
    this_month = months[0]
    if os.getenv("SKIP_HOLIDAYS", "true").lower() == "true":
        codes = [this_month.code(r, today) for r in this_month.people.values()]
        if codes and all(c == "HL" for c in codes):
            print("Skipped company holiday.")
            return
    status, state_row = sheets.state(today)
    if mode == "live" and status:
        print("Today's digest is already sent or awaiting delivery verification. No changes made.")
        return
    writes = []
    leaves = slack_requests(today, last_day)
    for leave in leaves:
        if leave.user not in mapping:
            raise DigestError("An active Slack applicant is unmapped; update Employee Mapping in the private tracker.")
        for month in months:
            writes.extend(month.plan(leave, mapping[leave.user], today, last_day))
    text = make_digest(months, today, last_day)
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
    webhook = required("TEAMS_WEBHOOK_URL")
    parsed = urlparse(webhook)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise DigestError("TEAMS_WEBHOOK_URL must be an HTTPS webhook URL.")
    # Refetch before writes to detect intervening edits. Sheets has no conditional cell write.
    for month in months:
        if sheets.read(month.title, f"A1:AZ{max_rows}") != originals[month.title]:
            raise DigestError("Calendar changed during the run; retry after reviewing privately.")
    if employee_mapping(sheets.read("Employee Mapping", "A1:B5000")) != mapping:
        raise DigestError("Employee Mapping changed during the run; retry after reviewing privately.")
    sheets.write(writes)
    # Reserve before POST. An ambiguous timeout must not cause an automatic duplicate.
    sheets.claim(today, state_row, "pending")
    response = requests.post(webhook, json=teams_payload(text), timeout=45, allow_redirects=False)
    if not 200 <= response.status_code < 300:
        raise DigestError("Teams did not accept the webhook; verify delivery privately before clearing pending state.")
    sheets.claim(today, state_row, "accepted")
    print("Calendar updated; Teams webhook accepted. Verify delivery in Teams/Power Automate run history.")


if __name__ == "__main__":
    try:
        main()
    except DigestError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
    except Exception:
        # Exception strings and tracebacks can contain webhook URLs, names, or API bodies.
        print("Run failed. Check credentials, configuration and private source data; no response bodies were logged.", file=sys.stderr)
        sys.exit(1)
