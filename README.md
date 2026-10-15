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
   | `SLACK_LEAVE_BOT_ID` | Optional: source workflow's `bot_id`, not the reader app's ID. Restricts ingestion to that workflow. If omitted, exact-template bot messages in this channel are accepted. |
   | `SPREADSHEET_ID` | Tracker ID from its Google Sheets URL |
   | `GOOGLE_CREDENTIALS_JSON` | Service account key JSON |
   | `TEAMS_WEBHOOK_URL` | Full signed Teams Workflows URL |

   Employee mappings live in the private tracker, not GitHub secrets. In **Actions → Team leave digest → Run workflow**, choose **setup-mapping** once. This creates an **Employee Mapping** tab with `Slack User ID` and `Tracker Name` columns and prefills names from the current month's roster. It never overwrites an existing mapping tab and never posts to Teams.

   Fill column A with each person's Slack member ID (profile → More → Copy member ID). Column B must match the calendar's exact name. Names without IDs can stay as placeholders, but every active Slack leave applicant needs a mapping before preview/live succeeds. Duplicate IDs, duplicate mapped names, invalid IDs and missing names stop the job. No employee mapping JSON secret or `users:read` scope is required.

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

Monthly tabs must be named `October 2026`, `November 2026`, etc. Row 1 contains exactly one `Name` header and every real date of that month as native spreadsheet date values. Names and calendar codes must be literal values; formulas in those cells fail validation rather than risking an overwrite. Other roster columns may precede the dates and may shift between months. Dates and names are found from headers, never fixed column positions. The read is bounded to `A1:AZ{CALENDAR_MAX_ROWS}`; keep the roster within that rectangle.

Recognized calendar codes: `PL` planned, `SL` sick/emergency, `HD` half day, `HL` holiday, `WK` weekend. `CPL` is excluded. Existing nonblank conflicting entries are never overwritten. A request covering a `WK`/`HL` cell leaves that cell unchanged.

The Slack template is:

```text
<@U012EXAMPLE> applied a Unplanned - Sick/Emergency Leave from 2026-10-15 to 2026-10-15. Full Day
Applied on Keka: NO
```

`Half Day` is also accepted. Formatting asterisks are tolerated. Keka application status does not determine availability. The mentioned member is the applicant, not the bot sender. The source combines sick/emergency, so the digest preserves that combined label. Existing `HD` cells do not encode planned versus unplanned; the summary gives them a separate half-day section without guessing. Leave reasons such as paternity are not inferred from `PL`.

The summary lists holidays from today through the configured horizon, today's planned/sick/half-day entries, and future planned dates. It compresses consecutive dates but preserves holiday/weekend gaps. Current planned entries show today's date rather than reconstructing a leave period that began before the reporting window.

## Recovery and limits

- The job re-reads recent Slack history each day, paginates completely and writes idempotently: existing matching `SL`/`HD` values produce no extra updates. Past dates are not backfilled. Requests posted earlier than the lookback, free-form messages and thread-only requests are outside this version. Use the workflow template in top-level channel messages.
- **Cancellations, deleted requests, and edits that retract/change a previously imported request need manual calendar reconciliation.** The importer does not automatically clear previously imported cells. If a live request remains in Slack, correcting only the sheet to blank will cause it to be imported again; correct the source as well. For automated cancellation support, add explicit request IDs/statuses to the source workflow and an audit ledger before enabling reversals.
- Requests arriving after the daily run appear on the next run. A same-day correction needs manual handling; the daily send guard intentionally prevents automatic repeat announcements.
- The job fails closed on unknown employees, changed templates, conflicting cells, missing months and incomplete history. Public errors contain configuration keys or generic diagnoses, not applicant names or API responses. Review the source Slack messages and tracker privately to resolve an error. Turn on Actions failure notifications.
- Calendar snapshots are checked again before writes to catch intervening edits, but Sheets does not provide a transaction across Slack, calendar updates and Teams. Avoid simultaneous manual calendar edits during the run. The workflow concurrency group serializes runs in this repository; do not run two repositories against the same tracker at once.
- `Digest State` is reserved for this job. `pending` is recorded before the Teams POST, and `accepted` after an HTTP success. A timeout or failure after reservation blocks automatic resends. **Inspect Teams and Power Automate history before deleting that date's state row to retry.** Never clear state merely because Actions failed: the message may already have arrived. If calendar writes succeeded but the send did not, the next run safely recognizes existing calendar codes.
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
