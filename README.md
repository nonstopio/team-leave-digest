# Team leave digest

A configurable GitHub Actions job that reads structured Slack leave requests, marks current/future sick or half-day leave in a monthly Google Sheets calendar, and sends a daily Adaptive Card to Teams. No AI service is required. Copy the repository and supply your own configuration to reuse it.

**The schedule is disabled until `ENABLE_SCHEDULE=true` is set. Manual runs default to preview. No live credentials or employee records belong in this repository.**

## Setup

1. Invite a Slack app to the leave channel. Its **bot token** needs `channels:history` (public channel) or `groups:history` (private channel). Reinstall the app if scopes changed. Copy the channel ID from its link. This app only reads Slack; `chat:write` is not needed by this job.
2. In Google Cloud, enable the Google Sheets API, create a service account and JSON key, and share the tracker with the service account's `client_email` as Editor. Your organization may restrict key creation or sharing; follow its access process. Store the entire key JSON as a GitHub secret, never a committed file.
3. Create a Teams Workflows webhook using **Post to a channel when a webhook request is received** (Adaptive Card input), choose the destination and add a backup workflow owner. The job uses the standard `{type: "message", attachments: [...]}` card envelope. A custom flow may require a different schema. Set webhook authentication to match your integration; this version supports the signed URL, without Microsoft OAuth. Check the Power Automate run history as well as Teams: HTTP acceptance does not guarantee the downstream post succeeded.
4. Under **Settings → Secrets and variables → Actions**, create these repository secrets:

   | Secret | Value |
   | --- | --- |
   | `SLACK_BOT_TOKEN` | Installed app's Bot User OAuth Token (`xoxb-…`) |
   | `SLACK_CHANNEL_ID` | Channel to read |
   | `SLACK_LEAVE_BOT_ID` | Source workflow's `bot_id`, not the reader app's ID. Required for live runs; only messages from this bot are imported. |
   | `SPREADSHEET_ID` | Tracker ID from its Google Sheets URL |
   | `GOOGLE_CREDENTIALS_JSON` | Service account key JSON |
   | `TEAMS_WEBHOOK_URL` | Full signed Teams Workflows URL for the **leadership** channel |
   | `ALERT_WEBHOOK_URL` | Teams Workflows URL for the **owners'** failure/conflict channel. Required for live runs. There is no fallback to the leadership URL, on purpose. |

   The webhook must be a Teams **Workflows** URL. Retired Office 365 connector URLs (`outlook.office.com`, `*.webhook.office.com`) are rejected. The Workflow runs as the person who created it; create it from a shared account and name a backup owner, or alerts stop silently if that person leaves.

   **Rollout:** while testing, set both secrets to the same test-channel URL. Before pointing `TEAMS_WEBHOOK_URL` at the real leadership channel, make sure `ALERT_WEBHOOK_URL` is the owners' channel, then change only `TEAMS_WEBHOOK_URL`.

   Employee mappings live in the private tracker, not GitHub secrets. In **Actions → Team leave digest → Run workflow**, choose **setup-mapping** once. This creates an **Employee Mapping** tab with `Slack User ID` and `Tracker Name` columns and prefills names from the current month's roster. It never overwrites an existing mapping tab and never posts to Teams.

   Fill column A with each person's Slack member ID (profile → More → Copy member ID). Column B must match the calendar's exact name. Names without IDs can stay as placeholders, but Slack applicants who are not in the mapping are treated as outside this team: they are ignored and only a count is logged. A team member whose Slack ID is missing is therefore also ignored, so keep the IDs complete. Duplicate IDs, duplicate mapped names, invalid IDs and missing names stop the job. No employee mapping JSON secret or `users:read` scope is required.

5. Set repository **variables** as desired:

   | Variable | Default | Meaning |
   | --- | --- | --- |
   | `ENABLE_SCHEDULE` | unset | Set to `true` only after preview and a successful live test |
   | `TIMEZONE` | `Asia/Kolkata` | Timezone for dates; changing this does not change cron |
   | `UPCOMING_DAYS` | `0` | 0 = rest of month; 1–90 = rolling calendar days, loading subsequent monthly tabs |
   | `SKIP_WEEKENDS` | `true` | Skip Saturday and Sunday |
   | `SKIP_HOLIDAYS` | `true` | Skip if all employee rows have `HL` today |
   | `SLACK_LOOKBACK_DAYS` | `90` | Read requests posted in the last 90 days, including multi-day requests still active |
   | `CALENDAR_MAX_ROWS` | `500` | Bounded calendar read; increase if the roster reaches this limit |

   Sensitive identifiers can stay in secrets. Timezone/range/weekend variables are intentionally non-sensitive. Posting time is the cron expression in `.github/workflows/digest.yml`, because GitHub schedule expressions cannot reference repository variables. Default: **12:07 PM IST, Monday–Friday** (`37 6 * * 1-5`, UTC). Change this to the agreed afternoon time before enabling.

## Preview, then enable

1. Create and fill **Employee Mapping** as described above, then open **Actions → Team leave digest → Run workflow → preview**. It validates access, mapping, dates and conflicts, and prints counts only. It does not write to Sheets or call Teams. A missing credential, unmatched applicant or conflict stops the run without writes.
2. To inspect the actual message privately, run locally in a trusted environment with the same environment variables. Set `RUN_MODE=preview` and `PRIVATE_PREVIEW_PATH=preview.txt`, then `python digest.py`. The file is created with owner-only permissions, excluded from git, and never uploaded. This output option is blocked inside GitHub Actions. Do not paste employee data into public issues, PRs or run logs.
3. Choose **live** for an initial end-to-end run when ready. This updates only blank cells for current/future requests and posts to Teams. It creates a `Digest State` tab in the tracker to record daily delivery status. Verify the sheet and Teams message privately.
4. Set `ENABLE_SCHEDULE=true` after verifying the result. Protect the default branch and restrict who can edit workflows or secrets. This privileged workflow runs only on schedule/manual dispatch, not on pull requests. Keep credentials out of third-party PR execution.

If the workflow is not visible yet, it must first exist on the default branch. A fork receives code, not the original repository's secret values; each team must configure its own credentials and tracker.

## Calendar and request format

Monthly tabs must be named `October 2026`, `November 2026`, etc. Row 1 contains exactly one `Name` header and every real date of that month as native spreadsheet date values. Names may be master-linked formulas: the job reads their evaluated values for employee matching. Leave-code cells must remain literal values; formulas there fail validation rather than risking an overwrite. A blank or failed name lookup stops the job. Other roster columns may precede the dates and may shift between months. Dates and names are found from headers, never fixed column positions. The read is bounded to `A1:AZ{CALENDAR_MAX_ROWS}`; keep the roster within that rectangle.

Recognized calendar codes: `PL` planned, `SL` sick/emergency, `HD` half day, `HL` holiday, `WK` weekend. `CPL` is excluded. Any other non-empty code on today's date is listed under **Other Entries** as unrecognized, never dropped. Existing nonblank conflicting entries are never overwritten. A request covering a `WK`/`HL` cell leaves that cell unchanged, and Saturday/Sunday cells are never written even if blank. A full-day `SL` already covers a half-day request for the same day. Each digest section is capped at 50 items (`...and N more`) and the card at 24 KB; a larger card stops the run before any write. The job logs a count of bot messages it did not recognise as leave requests, so a reworded Slack template is visible.

The Slack template is:

```text
<@U012EXAMPLE> applied a Unplanned - Sick/Emergency Leave from 2026-10-15 to 2026-10-15. Full Day
Applied on Keka: NO
```

`Half Day` is also accepted. Formatting asterisks are tolerated. Keka application status does not determine availability. The mentioned member is the applicant, not the bot sender. The source combines sick/emergency, so the digest preserves that combined label. Existing `HD` cells do not encode planned versus unplanned; the summary gives them a separate half-day section without guessing. Leave reasons such as paternity are not inferred from `PL`.

The leadership message is plain text in the established format (`Leaves: 15th October 2026`, numbered sections, dates like `19-30th Oct`). The summary lists holidays from today through the configured horizon, today's planned/sick/half-day entries, and future planned dates. It compresses consecutive dates but preserves holiday/weekend gaps. Current planned entries show today's date rather than reconstructing a leave period that began before the reporting window.

## Failure and conflict alerts

In live mode, if the run cannot safely produce a correct digest, **nothing is posted to leadership** and a card is posted to `ALERT_WEBHOOK_URL` instead. The job still exits 1. The alert names the offending record (employee, date and codes, or the Slack message ts and user ID, never message text), so **the alert channel must be private to the owners and must never be the leadership channel**. Those details appear only in the alert card, never in Actions logs or the leadership message. Previews and expected skips (weekend, holiday, already sent) do not alert. A final workflow step alerts when the job fails before `digest.py` can (install, tests, crash).

| Alert | Meaning | What to do |
| --- | --- | --- |
| Conflict | Conflicting calendar cell, unsupported Slack format, bad date | Fix Slack or the tracker privately, then re-run. A stuck conflict alerts every run until fixed. |
| Config | Sheets/Slack access, missing month tab, bad variable, webhook rejected | Fix the configuration, then re-run. Create next month's tab before the 1st working day. |
| Stale | Calendar or mapping edited during the run | Safe to re-run. |
| Delivery needs review | Teams outcome unknown (timeout), a state write failed, or a re-run found today's state `pending`/unrecognised | Check the leadership channel and Power Automate history **before** clearing today's `Digest State` row. |

A transient Teams rejection (429/5xx) is retried once. If Teams definitively rejects the post, the day is marked `failed`; fix the webhook and re-run today's `live` job manually (state is per date, so the next scheduled run does not retry today). A timeout stays `pending` and blocks resends; re-running while `pending` alerts instead of exiting quietly. A **manual `live` run posts to leadership and uses up that day's send**, so the scheduled run is skipped; use `preview` unless you mean it.

## Recovery and limits

- The job re-reads recent Slack history each day, paginates completely and writes idempotently: existing matching `SL`/`HD` values produce no extra updates. Past dates are not backfilled. Requests posted earlier than the lookback, free-form messages and thread-only requests are outside this version. Use the workflow template in top-level channel messages.
- **Cancellations, deleted requests, and edits that retract/change a previously imported request need manual calendar reconciliation.** The importer does not automatically clear previously imported cells. If a live request remains in Slack, correcting only the sheet to blank will cause it to be imported again; correct the source as well. For automated cancellation support, add explicit request IDs/statuses to the source workflow and an audit ledger before enabling reversals.
- Requests arriving after the daily run appear on the next run. A same-day correction needs manual handling; the daily send guard intentionally prevents automatic repeat announcements.
- A request in an unreadable format is skipped (and counted in the log) only if it has one `from YYYY-MM-DD to YYYY-MM-DD` clause, both dates are valid, and both are already past, because it can no longer affect the calendar or digest; with a current or future date, a missing, invalid or ambiguous date clause, the job fails closed. The job fails closed on a mapped employee missing from the calendar, changed templates, conflicting cells, missing months and incomplete history. Public errors contain configuration keys or generic diagnoses, not applicant names or API responses. Review the source Slack messages and tracker privately to resolve an error. Failures also post to the alert channel; keep Actions failure notifications on as a second signal.
- Calendar snapshots are checked again before writes to catch intervening edits, but Sheets does not provide a transaction across Slack, calendar updates and Teams. Avoid simultaneous manual calendar edits during the run. The workflow concurrency group serializes runs in this repository; do not run two repositories against the same tracker at once.
- `Digest State` is reserved for this job. `pending` is recorded before the Teams POST, `accepted` after an HTTP success, and `failed` after a definite rejection (a manual same-day live re-run retries). A timeout or ambiguous failure leaves `pending` and blocks automatic resends. **Inspect Teams and Power Automate history before deleting that date's state row to retry.** Never clear state merely because Actions failed: the message may already have arrived. If calendar writes succeeded but the send did not, the next run safely recognizes existing calendar codes.
- GitHub schedules may be delayed or dropped under load, and public-repository schedules disable after 60 days without repository activity. Monitor run history; use an external scheduler if timing or unattended continuity is critical. See [GitHub schedule behavior](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).
- Public logs and artifacts must not contain previews, employee mappings, HTTP bodies or spreadsheet exports. The application suppresses raw exception details; never add debug HTTP logging to production. Pin/review dependencies and third-party action updates before deploying them.

## Development

Python 3.12:

```sh
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

Tests use fictional records and no credentials. The pure parser/calendar tests also run without installing API dependencies. No external services are contacted by tests.
