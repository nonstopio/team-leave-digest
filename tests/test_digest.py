import calendar
import json
import unittest
import copy
import os
import sys
from types import SimpleNamespace
from unittest.mock import Mock, patch
from datetime import date, timedelta
import digest
from digest import DigestError, Leave, Month, column, date_list, employee_mapping, build_card, digest_card, digest_text, make_digest, parse_request, shadow_names, team_labels, webhook_url


def month(name_col=2):
    start = date(2026, 10, 1)
    headers = [''] * name_col + ['Name'] + [(start + timedelta(days=i) - date(1899, 12, 30)).days for i in range(31)]
    return Month('October 2026', [headers, [''] * name_col + ['Sample Employee'] + [''] * 31], 2026, 10)


def cal_rows(year, month_no, name_col=2):
    first, days = date(year, month_no, 1), calendar.monthrange(year, month_no)[1]
    headers = [''] * name_col + ['Name'] + [(first + timedelta(days=i) - date(1899, 12, 30)).days for i in range(days)]
    return [headers, [''] * name_col + ['Sample Employee'] + [''] * days]


def text_of(months, today, hidden=frozenset(), teams=None):
    return digest_text(make_digest(months, today, hidden, teams))


def walk(element):
    """Every dict inside a card, depth first."""
    if isinstance(element, dict):
        yield element
        for value in element.values():
            yield from walk(value)
    elif isinstance(element, list):
        for value in element:
            yield from walk(value)


def cell_texts(table):
    return [[c['items'][0]['text'] for c in row['cells']] for row in table['rows']]


class DigestTests(unittest.TestCase):
    def test_mapping_accepts_unfilled_roster_and_trims_ids(self):
        self.assertEqual(employee_mapping([['Slack User ID', 'Tracker Name'], ['', 'Unmapped Person'],
                                           [' U123 ', 'Sample Employee']]), {'U123': 'Sample Employee'})

    def test_mapping_rejects_duplicates_and_bad_rows(self):
        for row in [['U123', 'Other'], ['U456', 'sample employee'], ['@sample', 'Other'], ['U789', '']]:
            with self.assertRaises(DigestError):
                employee_mapping([['Slack User ID', 'Tracker Name'], ['U123', 'Sample Employee'], row])
        with self.assertRaises(DigestError):
            employee_mapping([['Wrong', 'Headers']])

    def test_workflow_mention_not_bot_author(self):
        msg = {'bot_id': 'BSOURCE', 'user': 'UBOT', 'text':
               '<@U123> applied a *Unplanned - Sick/Emergency Leave* from 2026-10-15 to 2026-10-15. Full Day\nApplied on Keka: NO'}
        self.assertEqual(parse_request(msg), Leave('U123', date(2026, 10, 15), date(2026, 10, 15), 'SL'))
        self.assertIsNone(parse_request({'text': msg['text']}))

    def test_half_day_and_invalid_dates(self):
        msg = {'bot_id': 'B1', 'text': '<@U123> applied a Unplanned - Sick/Emergency Leave from 2026-10-15 to 2026-10-15. Half Day'}
        self.assertEqual(parse_request(msg).code, 'HD')
        msg['text'] = msg['text'].replace('to 2026-10-15', 'to 2026-10-14')
        with self.assertRaises(DigestError):
            parse_request(msg)

    def test_changed_template_is_not_silently_ignored(self):
        with self.assertRaises(DigestError):
            parse_request({'bot_id': 'B1', 'text': '<@U123> applied a Unplanned leave with a new format'})

    def test_header_discovery_idempotence_and_no_past_writes(self):
        m = month(1)
        today = date(2026, 10, 15)
        leave = Leave('U123', today - timedelta(days=2), today, 'SL')
        writes = m.plan(leave, 'sample employee', today, date(2026, 10, 31))
        self.assertEqual(writes, [{'range': "'October 2026'!Q2", 'values': [['SL']]}])
        self.assertEqual(m.plan(leave, 'Sample Employee', today, today), [])

    def test_conflicts_fail_and_holidays_are_preserved(self):
        m, today = month(), date(2026, 10, 15)
        leave = Leave('U123', today, today, 'SL')
        m.rows[1][m.dates[today]] = 'PL'
        with self.assertRaises(DigestError):
            m.plan(leave, 'Sample Employee', today, today)
        m.rows[1][m.dates[today]] = 'HL'
        self.assertEqual(m.plan(leave, 'Sample Employee', today, today), [])

    def test_missing_employee_and_dates_fail(self):
        m, today = month(), date(2026, 10, 15)
        with self.assertRaises(DigestError):
            m.plan(Leave('U123', today, today, 'SL'), 'Unknown', today, today)
        with self.assertRaises(DigestError):
            Month('October 2026', [['Name'], ['Sample Employee']], 2026, 10)

    def test_formula_is_not_overwritten(self):
        m = month()
        m.rows[1][m.dates[date(2026, 10, 15)]] = '=IF(TRUE,"","")'
        with self.assertRaises(DigestError):
            Month(m.title, m.rows, 2026, 10)

    def test_master_linked_name_uses_evaluated_value(self):
        m = month()
        evaluated = copy.deepcopy(m.rows)
        m.rows[1][m.name_col] = '=XLOOKUP(AI2,Master!A:A,Master!B:B)'
        linked = Month(m.title, m.rows, 2026, 10, evaluated=evaluated)
        self.assertEqual(linked.people, {'sample employee': 1})
        self.assertEqual(linked.rows[1][linked.name_col], 'Sample Employee')

    def test_unresolved_master_name_fails(self):
        for value in ['', '#N/A', 'MISSING IN MASTER']:
            m = month()
            evaluated = copy.deepcopy(m.rows)
            m.rows[1][m.name_col] = '=XLOOKUP(AI2,Master!A:A,Master!B:B)'
            evaluated[1][m.name_col] = value
            with self.assertRaises(DigestError):
                Month(m.title, m.rows, 2026, 10, evaluated=evaluated)

    def test_summary_ignores_cancelled_and_groups_exact_dates(self):
        m, today = month(), date(2026, 10, 15)
        for day, code in [(15, 'SL'), (19, 'PL'), (20, 'HL'), (21, 'PL'), (22, 'CPL')]:
            m.rows[1][m.dates[date(2026, 10, day)]] = code
        text = text_of([m], today)
        self.assertIn('Sick/Emergency leave', text)
        self.assertIn('19th Oct, 21st Oct', text)
        self.assertNotIn('22 Oct', text)
        self.assertIn('Tuesday, October 20, 2026', text)
        self.assertIn('Leaves: 15th October 2026', text)

    def test_utilities_and_card(self):
        self.assertEqual(column(34), 'AH')
        self.assertEqual(date_list([date(2026, 10, 19), date(2026, 10, 20), date(2026, 10, 30)]), '19th-20th Oct, 30th Oct')
        self.assertEqual(date_list([date(2026, 10, 19)]), '19th Oct')
        self.assertEqual(date_list([date(2026, 10, 30), date(2026, 10, 31), date(2026, 11, 1)]), '30th Oct-1st Nov')
        self.assertEqual([digest.ordinal(n) for n in (1, 2, 3, 4, 11, 12, 13, 21, 22, 23)],
                         ['1st', '2nd', '3rd', '4th', '11th', '12th', '13th', '21st', '22nd', '23rd'])
        self.assertEqual(build_card('T', 'good', 'Sample')['attachments'][0]['content']['type'], 'AdaptiveCard')

    def test_card_keeps_raw_text_and_uses_tables(self):
        m, today = month(), date(2026, 10, 15)
        m.rows[1][m.name_col] = "O'Brien & Sons"
        m.rows[1][m.dates[today]] = 'SL'
        m.rows[1][m.dates[date(2026, 10, 19)]] = 'PL'
        m.rows[1][m.dates[date(2026, 10, 20)]] = 'PL'
        m = Month(m.title, m.rows, 2026, 10)
        card = digest_card(make_digest([m], today))
        content = card['attachments'][0]['content']
        self.assertTrue(content['$schema'].startswith('http'))
        self.assertEqual(content['version'], '1.5')
        header = content['body'][0]
        self.assertEqual((header['type'], header['style']), ('Container', 'emphasis'))
        self.assertEqual(header['items'][0]['text'], 'Leaves: 15th October 2026')
        self.assertNotIn('actions', content)
        self.assertNotIn('[TEST]', str(content))
        texts = [b.get('text') for b in content['body']]
        self.assertFalse(any('&amp;' in str(t) or '&#x27;' in str(t) for t in texts))
        self.assertIn('Sick/Emergency Leaves (1)', texts)
        self.assertIn('Upcoming Leaves · next 5 working days (1)', texts)
        tables = [b for b in content['body'] if b['type'] == 'Table']
        self.assertEqual(cell_texts(tables[0]), [['#', 'Name', 'Details'], ['', 'No team (1)', ''],
                                                 ['1', "O'Brien & Sons", 'Sick/Emergency leave']])
        self.assertEqual(cell_texts(tables[1])[-1], ['1', "O'Brien & Sons", '19th-20th Oct'])
        self.assertEqual(cell_texts(tables[1])[0], ['#', 'Name', 'Dates'])
        # empty sections stay visible but quiet
        i = texts.index('Half-day Leaves')
        self.assertEqual((texts[i + 1], content['body'][i + 1]['isSubtle']), ('None', True))

    def test_shadow_roles_are_found_by_header_and_hidden_from_digest(self):
        rows = [['Slack User ID', 'Tracker Name', 'Role', 'Team'], ['U1', 'Sample Employee', 'Shadow QA', 'V1'],
                ['', 'Placeholder Person', 'shadow developer', 'V1'], ['U2', 'Other Person', 'Backend', 'V1'],
                ['U3', 'Lead Person', 'Lead', 'V1']]
        self.assertEqual(shadow_names(rows), {'sample employee', 'placeholder person'})
        with self.assertRaises(DigestError):  # no Role column: fail closed rather than expose Shadow people
            shadow_names([['Slack User ID', 'Tracker Name']])
        self.assertEqual(digest.employee_mapping(rows), {'U1': 'Sample Employee', 'U2': 'Other Person', 'U3': 'Lead Person'})
        m, today = month(), date(2026, 10, 15)
        m.rows[1][m.dates[today]] = 'SL'
        self.assertIn('Sample Employee', text_of([m], today))
        self.assertNotIn('Sample Employee', text_of([m], today, shadow_names(rows)))

    def test_team_is_appended_to_names_in_the_preview_text(self):
        rows = [['Slack User ID', 'Tracker Name', 'Role', 'Team'], ['U1', 'Sample Employee', 'QA', 'INT'], ['U2', 'No Team', 'QA', '']]
        self.assertEqual(team_labels(rows), {'sample employee': 'INT'})
        self.assertEqual(team_labels([['Slack User ID', 'Tracker Name']]), {})
        m, today = month(), date(2026, 10, 15)
        m.rows[1][m.dates[today]] = 'SL'
        m.rows[1][m.dates[date(2026, 10, 19)]] = 'PL'
        text = text_of([m], today, teams=team_labels(rows))
        self.assertIn('Sample Employee (INT): Sick/Emergency leave', text)
        self.assertIn('Sample Employee (INT): 19th Oct', text)
        self.assertIn('Sample Employee: Sick/Emergency leave', text_of([m], today))  # no Team column: unchanged

    def test_placeholder_duplicate_cannot_hide_a_real_employee(self):
        rows = [['Slack User ID', 'Tracker Name', 'Role'], ['U1', 'Sample Employee', 'QA'], ['', 'sample  employee', 'Shadow QA']]
        with self.assertRaises(DigestError):
            digest.employee_mapping(rows)

    def test_full_day_request_upgrades_an_existing_half_day(self):
        m, today = month(), date(2026, 10, 15)
        m.rows[1][m.dates[today]] = 'HD'
        writes = m.plan(Leave('U123', today, today, 'SL'), 'Sample Employee', today, today)
        self.assertEqual([w['values'] for w in writes], [[['SL']]])
        m.rows[1][m.dates[today]] = 'PL'
        with self.assertRaises(DigestError):  # a planned leave is still a conflict
            m.plan(Leave('U123', today, today, 'SL'), 'Sample Employee', today, today)

    def test_free_text_in_a_cell_never_reaches_leadership(self):
        m, today = month(), date(2026, 10, 15)
        m.rows[1][m.dates[today]] = 'Sick - doctor appt, private'
        text = text_of([m], today)
        self.assertIn('Other Entries', text)
        self.assertNotIn('doctor', text)
        self.assertIn('Sample Employee: unrecognized entry (unrecognized code)', text)
        m.rows[1][m.dates[today]] = 'ml'
        self.assertIn('Sample Employee: ML (unrecognized code)', text_of([m], today))

    def test_newline_in_a_name_stays_on_one_line(self):
        m, today = month(), date(2026, 10, 15)
        m.rows[1][m.name_col] = 'Ann\n\nSmith'
        m.rows[1][m.dates[today]] = 'SL'
        m = Month(m.title, m.rows, 2026, 10)
        self.assertIn('1. Ann Smith: Sick/Emergency leave', text_of([m], today))
        self.assertIn('Ann Smith', cell_texts(next(e for e in walk(digest_card(make_digest([m], today))) if e.get('type') == 'Table'))[2])

    def test_absurd_date_header_is_a_clean_error(self):
        rows = month().rows
        rows[0][-1] = 99999999999
        with self.assertRaises(DigestError):
            Month('October 2026', rows, 2026, 10)

    def test_alert_card_has_attention_header_and_run_link(self):
        card = build_card('Leave digest NOT sent', 'attention', 'Fix it.', 'https://example.invalid/run/1')
        content = card['attachments'][0]['content']
        self.assertEqual(content['body'][0]['style'], 'attention')
        self.assertEqual(content['actions'][0]['type'], 'Action.OpenUrl')

    def test_conflict_detail_is_separate_from_public_message(self):
        m, today = month(), date(2026, 10, 15)
        m.rows[1][m.dates[today]] = 'PL'
        with self.assertRaises(DigestError) as ctx:
            m.plan(Leave('U123', today, today, 'SL'), 'Sample Employee', today, today)
        self.assertNotIn('Sample Employee', str(ctx.exception))
        self.assertEqual(ctx.exception.detail, 'Sample Employee, 15 Oct 2026: tracker has PL, Slack requested SL')

    def test_personal_work_is_ignored(self):
        for duration in ('Half Day', 'Full Day'):
            msg = {'bot_id': 'B1', 'ts': '1', 'text':
                   f'<@U1|Rushi> applied a *Unplanned - Personal Work* from 2026-10-26 to 2026-10-26. {duration}\n\nApplied on Keka: YES'}
            self.assertIsNone(parse_request(msg, date(2026, 10, 26)))

    def test_unparseable_request_is_skipped_only_when_provably_over(self):
        today = date(2026, 10, 15)
        def msg(text):
            return {'bot_id': 'B1', 'ts': '1', 'text': text}
        # unsupported wording, every date already past: harmless, skipped
        self.assertIsNone(parse_request(msg('<@U1> applied a Unplanned - WFH from 2026-09-24 to 2026-09-25.'), today))
        # a date today or later, or no date at all, or no 'today' context: still fails closed
        for text in ['<@U1> applied a Unplanned - WFH from 2026-09-24 to 2026-10-15.',
                     '<@U1> applied a Unplanned - WFH from 2026-09-24 to 2026-11-02.',
                     '<@U1> applied a Unplanned something odd']:
            with self.assertRaises(DigestError):
                parse_request(msg(text), today)
        with self.assertRaises(DigestError):
            parse_request(msg('<@U1> applied a Unplanned - WFH from 2026-09-24 to 2026-09-25.'))
        # impossible dates fail closed, even if the other date is past
        with self.assertRaises(DigestError):
            parse_request(msg('<@U1> applied a Unplanned - Sick/Emergency Leave from 2026-09-31 to 2026-09-24. Full Day'), today)
        # a stray past date elsewhere must not hide a leave in another date format
        with self.assertRaises(DigestError):
            parse_request(msg('<@U1> applied a Unplanned - WFH on 2026-09-24 from 24/10/2026 to 26/10/2026.'), today)
        # two ranges in one message are ambiguous: fail closed
        with self.assertRaises(DigestError):
            parse_request(msg('<@U1> applied a Unplanned - WFH from 2026-09-24 to 2026-09-25 and from 2026-10-20 to 2026-10-21.'), today)
        # reversed range that ended in the past is skipped
        self.assertIsNone(parse_request(msg('<@U1> applied a Unplanned - Sick/Emergency Leave from 2026-09-26 to 2026-09-24. Full Day'), today))
        # a valid current request is unaffected
        self.assertEqual(parse_request(msg('<@U1> applied a Unplanned - Sick/Emergency Leave from 2026-10-15 to 2026-10-16. Half Day'), today).code, 'HD')

    def test_slack_history_skips_ended_unparseable_but_not_current(self):
        old = {'bot_id': 'BSOURCE', 'ts': '1', 'text': '<@U1> applied a Unplanned - WFH from 2026-09-24 to 2026-09-25.'}
        ok = {'bot_id': 'BSOURCE', 'text': SlackAndStateTests.TEXT}
        self.assertEqual(len(SlackAndStateTests().history([{'ok': True, 'messages': [old, ok]}])), 1)

    def test_unsupported_format_detail_has_ids_not_message_text(self):
        with self.assertRaises(DigestError) as ctx:
            parse_request({'bot_id': 'B1', 'ts': '1760500000.1', 'user': 'U9', 'text': '<@U123> applied a Unplanned reason: private'})
        self.assertEqual(ctx.exception.detail, 'Slack message ts 1760500000.1, user U9')
        self.assertIn('1760500000.1', str(ctx.exception))  # ts helps find the message; it is not sensitive
        self.assertNotIn('U9', str(ctx.exception))
        self.assertNotIn('private', ctx.exception.detail)

    def test_weekend_cells_are_not_written(self):
        m = month()  # 2026-10-16 is a Friday, 17-18 weekend, 19 Monday
        writes = m.plan(Leave('U123', date(2026, 10, 16), date(2026, 10, 19), 'SL'), 'Sample Employee',
                        date(2026, 10, 16), date(2026, 10, 31))
        self.assertEqual(len(writes), 2)

    def test_full_day_covers_half_day_request(self):
        m, today = month(), date(2026, 10, 15)
        self.assertEqual(len(m.plan(Leave('U123', today, today, 'SL'), 'Sample Employee', today, today)), 1)
        self.assertEqual(m.plan(Leave('U123', today, today, 'HD'), 'Sample Employee', today, today), [])

    def test_unknown_code_is_shown_not_dropped(self):
        m, today = month(), date(2026, 10, 15)
        m.rows[1][m.dates[today]] = 'ML'
        text = text_of([m], today)
        self.assertIn('Other Entries', text)
        self.assertIn('Sample Employee: ML', text)
        self.assertNotIn('Other Entries', text_of([month()], today))
        self.assertNotIn('Other Entries', str(digest_card(make_digest([month()], today))))

    def test_webhook_url_rejects_unsafe_values(self):
        for bad in ['http://example.invalid/x', 'https://u:p@example.invalid/x',
                    'https://outlook.office.com/webhook/abc', 'https://contoso.webhook.office.com/x']:
            with patch.dict(os.environ, {'HOOK': bad}), self.assertRaises(DigestError):
                webhook_url('HOOK')
        with patch.dict(os.environ, {'HOOK': 'https://prod.logic.azure.com/x'}):
            self.assertEqual(webhook_url('HOOK'), 'https://prod.logic.azure.com/x')


class MainTests(unittest.TestCase):
    def run_job(self, mode='preview', status=None, post_status=202, changed=False, leaves=None, post_error=None,
                alert_status=202, env_extra=None, post_statuses=None, claim_error_on=None, rows_edit=None, now=date(2026, 10, 15), mapping_extra=None, mapping_later=None, titles=('October 2026', 'November 2026'), nov_edit=None, nov_outage=False, tabs=None):
        rows = month().rows
        sheets = Mock()
        sheets.titles = set(titles) | {'Employee Mapping', 'Digest State'}
        mapping_rows = mapping_extra or [['Slack User ID', 'Tracker Name', 'Role', 'Team'], ['U123', 'Sample Employee', 'QA', '']]
        mapping_reads, reads = [], []
        sheets.reads = reads

        nov_rows = cal_rows(2026, 11)
        if nov_edit:
            nov_edit(nov_rows)

        def read(title, area, render='FORMULA'):
            reads.append(title)
            if title == 'November 2026' and nov_outage:
                raise DigestError('Google Sheets API failed', 'sheets')
            if title in (tabs or {}) and title in sheets.titles:
                return copy.deepcopy(tabs[title])
            if title == 'November 2026' and title in sheets.titles:
                return copy.deepcopy(nov_rows)
            if title != 'Employee Mapping':
                if title not in sheets.titles:
                    raise DigestError('A required sheet tab is missing.')
                return copy.deepcopy(rows)
            mapping_reads.append(1)
            later = mapping_later is not None and len(mapping_reads) > 1
            return copy.deepcopy(mapping_later if later else mapping_rows)
        sheets.read.side_effect = read
        sheets.state.return_value = (status, 2)
        if changed:
            modified = copy.deepcopy(rows)
            modified[1][2] = 'Renamed Employee'
            sheets.read.side_effect = [mapping_rows, copy.deepcopy(rows), modified]
        clock = Mock()
        clock.now.return_value.date.return_value = now
        if claim_error_on:
            sheets.claim.side_effect = lambda d, r, status: (_ for _ in ()).throw(DigestError('Sheets API failed')) \
                if status == claim_error_on else None
        if rows_edit:
            rows_edit(rows)
        def post(url, **kwargs):
            if url.endswith('/alert'):
                return SimpleNamespace(status_code=alert_status)
            if post_error:
                raise post_error
            return SimpleNamespace(status_code=next(statuses))
        statuses = iter(post_statuses or [post_status] * 5)
        request = Mock(side_effect=post)
        env = {'RUN_MODE': mode,
               'TEAMS_WEBHOOK_URL': 'https://example.invalid/leadership',
               'ALERT_WEBHOOK_URL': 'https://example.invalid/alert',
               'SLACK_LEAVE_BOT_ID': 'BSOURCE', **(env_extra or {})}
        leave = Leave('U123', date(2026, 10, 15), date(2026, 10, 15), 'SL')
        with patch.dict(os.environ, env, clear=True), patch.object(digest, 'datetime', clock), \
             patch.object(digest, 'Sheets', return_value=sheets), patch.object(digest.time, 'sleep'), \
             patch.object(digest, 'slack_requests', return_value=leaves if leaves is not None else [leave]), \
             patch.dict(sys.modules, {'requests': SimpleNamespace(post=request, RequestException=OSError)}):
            error = None
            try:
                digest.main()
            except DigestError as exc:
                error = exc
        return sheets, request, error

    def test_preview_never_writes_or_posts(self):
        sheets, post, error = self.run_job()
        self.assertIsNone(error)
        sheets.write.assert_not_called()
        sheets.claim.assert_not_called()
        post.assert_not_called()

    def test_live_reserves_and_accepts_once(self):
        sheets, post, error = self.run_job('live')
        self.assertIsNone(error)
        self.assertEqual(sheets.claim.call_args_list[0].args[-1], 'pending')
        self.assertEqual(sheets.claim.call_args_list[1].args[-1], 'accepted')
        post.assert_called_once()

    def test_accepted_state_skips_quietly_without_alert(self):
        sheets, post, error = self.run_job('live', status='accepted')
        self.assertIsNone(error)
        sheets.write.assert_not_called()
        post.assert_not_called()

    @staticmethod
    def posted(post):
        return [c.args[0].rsplit('/', 1)[1] for c in post.call_args_list]

    def test_rejected_post_marks_failed_for_a_manual_rerun(self):
        sheets, post, error = self.run_job('live', post_status=400)
        self.assertEqual(error.kind, 'config')
        self.assertIn('re-run', str(error))
        self.assertEqual([c.args[-1] for c in sheets.claim.call_args_list], ['pending', 'failed'])
        self.assertEqual(self.posted(post), ['leadership', 'alert'])

    def test_rate_limit_is_retried_once_without_failing(self):
        sheets, post, error = self.run_job('live', post_statuses=[429, 202])
        self.assertIsNone(error)
        self.assertEqual(self.posted(post), ['leadership', 'leadership'])
        self.assertEqual(sheets.claim.call_args_list[-1].args[-1], 'accepted')

    def test_persistent_rate_limit_marks_failed_after_one_retry(self):
        sheets, post, error = self.run_job('live', post_statuses=[429, 429])
        self.assertEqual(error.kind, 'config')
        self.assertEqual(self.posted(post), ['leadership', 'leadership', 'alert'])
        self.assertEqual(sheets.claim.call_args_list[-1].args[-1], 'failed')

    def test_gateway_error_is_ambiguous_never_retried_never_failed(self):
        for code in (500, 502, 503, 504):
            sheets, post, error = self.run_job('live', post_status=code)
            self.assertEqual(error.kind, 'delivery')
            self.assertEqual(self.posted(post), ['leadership', 'alert'])  # no second post: it may have been delivered
            self.assertEqual([c.args[-1] for c in sheets.claim.call_args_list], ['pending'])  # stays pending

    def test_failed_state_write_after_rejection_alerts_as_delivery(self):
        sheets, post, error = self.run_job('live', post_status=400, claim_error_on='failed')
        self.assertEqual(error.kind, 'delivery')
        self.assertEqual(self.posted(post), ['leadership', 'alert'])

    def test_accepted_state_write_failure_alerts_as_delivery(self):
        sheets, post, error = self.run_job('live', claim_error_on='accepted')
        self.assertEqual(error.kind, 'delivery')

    def test_failed_state_is_retried_but_pending_is_not(self):
        sheets, post, error = self.run_job('live', status='failed')
        self.assertIsNone(error)
        self.assertEqual(self.posted(post), ['leadership'])

    def test_ambiguous_post_stays_pending_and_alerts_delivery(self):
        sheets, post, error = self.run_job('live', post_error=OSError())
        self.assertEqual(error.kind, 'delivery')
        sheets.claim.assert_called_once_with(date(2026, 10, 15), 2, 'pending')
        self.assertEqual(self.posted(post), ['leadership', 'alert'])

    def test_intervening_roster_edit_blocks_write_and_leadership_post(self):
        sheets, post, error = self.run_job('live', changed=True)
        self.assertEqual(error.kind, 'stale')
        sheets.write.assert_not_called()
        self.assertEqual(self.posted(post), ['alert'])

    @staticmethod
    def pl_today(rows):
        col = [i for i, v in enumerate(rows[0]) if v == (date(2026, 10, 15) - date(1899, 12, 30)).days][0]
        rows[1][col] = 'PL'  # existing planned leave on the day Slack says sick -> conflict

    def test_conflict_alerts_owners_only(self):
        sheets, post, error = self.run_job('live', rows_edit=self.pl_today)
        self.assertEqual(error.kind, 'conflict')
        sheets.write.assert_not_called()
        self.assertEqual(self.posted(post), ['alert'])
        body = str(post.call_args.kwargs['json'])
        self.assertIn('NOT sent', body)
        self.assertIn('Sample Employee, 15 Oct 2026: tracker has PL, Slack requested SL', body)  # owners' channel only
        self.assertNotIn('Sample Employee', str(error))  # ...never the public message/log line

    def test_shadow_leave_is_written_to_sheet_but_not_posted(self):
        mapping = [['Slack User ID', 'Tracker Name', 'Role', 'Team'], ['U123', 'Sample Employee', 'Shadow QA', 'V1']]
        sheets, post, error = self.run_job('live', mapping_extra=mapping)
        self.assertIsNone(error)
        self.assertEqual(len(sheets.write.call_args.args[0]), 1)  # sheet still updated
        card = str(post.call_args_list[0].kwargs['json'])
        self.assertNotIn('Sample Employee', card)
        self.assertIn('Sick/Emergency Leaves', card)

    def test_live_card_groups_by_team_without_name_suffix(self):
        mapping = [['Slack User ID', 'Tracker Name', 'Role', 'Team'], ['U123', 'Sample Employee', 'QA', 'INT']]
        sheets, post, error = self.run_job('live', mapping_extra=mapping)
        self.assertIsNone(error)
        body = post.call_args_list[0].kwargs['json']['attachments'][0]['content']['body']
        sick = body[[b.get('text') for b in body].index('Sick/Emergency Leaves (1)') + 1]
        self.assertEqual(cell_texts(sick)[1:], [['', 'INT (1)', ''], ['1', 'Sample Employee', 'Sick/Emergency leave']])

    def test_team_change_during_run_is_treated_as_stale(self):
        first = [['Slack User ID', 'Tracker Name', 'Role', 'Team'], ['U123', 'Sample Employee', 'QA', 'V1']]
        later = [['Slack User ID', 'Tracker Name', 'Role', 'Team'], ['U123', 'Sample Employee', 'QA', 'V2']]
        sheets, post, error = self.run_job('live', mapping_extra=first, mapping_later=later)
        self.assertEqual(error.kind, 'stale')
        sheets.write.assert_not_called()

    def test_role_change_during_run_is_treated_as_stale(self):
        first = [['Slack User ID', 'Tracker Name', 'Role'], ['U123', 'Sample Employee', 'QA']]
        later = [['Slack User ID', 'Tracker Name', 'Role'], ['U123', 'Sample Employee', 'Shadow QA']]
        sheets, post, error = self.run_job('live', mapping_extra=first, mapping_later=later)
        self.assertEqual(error.kind, 'stale')
        sheets.write.assert_not_called()
        self.assertEqual(self.posted(post), ['alert'])

    @staticmethod
    def spans(post):
        """The two window date labels of the leadership card."""
        body = post.call_args_list[-1].kwargs['json']['attachments'][0]['content']['body']
        return [e['text'] for e in walk(body) if e.get('type') == 'TextBlock' and e.get('isSubtle')
                and (' – ' in e['text'] or 'not available yet' in e['text'])]

    def test_window_stays_in_october_mid_month_and_reaches_next_month_late(self):
        sheets, post, error = self.run_job('live', now=date(2026, 10, 15))  # 10th working day is Oct 29
        self.assertNotIn('November 2026', sheets.reads)
        self.assertEqual(self.spans(post), ['Fri 16 Oct – Thu 22 Oct', 'Fri 23 Oct – Thu 29 Oct'])
        def nov_leave(rows):
            rows[1][[i for i, v in enumerate(rows[0]) if v == (date(2026, 11, 2) - date(1899, 12, 30)).days][0]] = 'PL'
        sheets, post, error = self.run_job('live', now=date(2026, 10, 26), nov_edit=nov_leave)  # 10th working day is Nov 9
        self.assertIsNone(error)
        self.assertIn('November 2026', sheets.reads)
        self.assertEqual(self.spans(post), ['Tue 27 Oct – Mon 2 Nov', 'Tue 3 Nov – Mon 9 Nov'])
        (table,) = [e for e in walk(post.call_args_list[-1].kwargs['json']) if e.get('type') == 'Table']
        self.assertEqual(cell_texts(table)[-1], ['1', 'Sample Employee', '2nd Nov'])  # in window 1, across month end

    def test_missing_or_invalid_next_month_falls_back_with_a_heads_up(self):
        sheets, post, error = self.run_job('live', now=date(2026, 10, 26), titles=('October 2026',))
        self.assertIsNone(error)
        self.assertEqual(self.posted(post), ['alert', 'leadership'])
        alert = str(post.call_args_list[0].kwargs['json'])
        self.assertIn('heads-up', alert)
        self.assertIn("'November 2026' calendar could not be used", alert)
        self.assertIn('only covers through 31 Oct', alert)
        self.assertNotIn('NOT sent', alert)
        self.assertEqual(alert.count('heads-up'), 1)  # one notice, not a second 'tab does not exist' alert
        self.assertEqual(self.spans(post), ["Tue 27 Oct – Fri 30 Oct · leaves after 31 Oct not available yet",
                                            "Leaves after 31 Oct not available yet"])
        # a tab that exists but is broken falls back the same way
        sheets, post, error = self.run_job('live', now=date(2026, 10, 26),
                                           nov_edit=lambda rows: rows.__setitem__(0, ['Name']))
        self.assertIsNone(error)
        self.assertEqual(self.posted(post), ['alert', 'leadership'])

    def test_sheets_outage_never_silently_truncates_the_window(self):
        sheets, post, error = self.run_job('live', now=date(2026, 10, 26), nov_outage=True)
        self.assertEqual(error.kind, 'sheets')
        sheets.write.assert_not_called()
        self.assertEqual(self.posted(post), ['alert'])  # no leadership post of a half-built digest
        self.assertIn('NOT sent', str(post.call_args.kwargs['json']))

    def test_heads_up_does_not_disable_the_early_failure_backstop(self):
        out = os.path.join(os.environ.get('TMPDIR', '/tmp'), 'gh_output_notice')
        open(out, 'w').close()
        self.run_job('live', now=date(2026, 10, 26), titles=('October 2026',), env_extra={'GITHUB_OUTPUT': out})
        with open(out) as f:
            self.assertEqual(f.read(), '')  # a notice alert must not write alerted=true
        self.run_job('live', rows_edit=self.pl_today, env_extra={'GITHUB_OUTPUT': out})
        with open(out) as f:
            self.assertEqual(f.read(), 'alerted=true\n')  # a real failure alert does
        os.remove(out)

    def test_alert_details_are_dropped_when_alert_channel_is_the_leadership_channel(self):
        same = 'https://example.invalid/leadership'
        sheets, post, error = self.run_job('live', rows_edit=self.pl_today, env_extra={'ALERT_WEBHOOK_URL': same})
        body = str(post.call_args.kwargs['json'])
        self.assertIn('NOT sent', body)
        self.assertNotIn('tracker has PL', body)
        self.assertNotIn('Sample Employee', body)

    def test_verify_is_quiet_when_todays_digest_was_accepted(self):
        sheets, post, error = self.run_job('verify', status='accepted')
        self.assertIsNone(error)
        post.assert_not_called()
        sheets.write.assert_not_called()
        sheets.claim.assert_not_called()

    def test_verify_alerts_the_owners_only_when_no_accepted_digest_exists(self):
        for status in (None, 'pending', 'failed'):
            sheets, post, error = self.run_job('verify', status=status)
            self.assertEqual(error.kind, 'missing')
            self.assertEqual(self.posted(post), ['alert'])  # never the leadership channel
            sheets.write.assert_not_called()
            sheets.claim.assert_not_called()
            self.assertIn('NOT sent', str(post.call_args.kwargs['json']))

    def test_verify_skips_weekends_and_holidays_quietly(self):
        sheets, post, error = self.run_job('verify', now=date(2026, 10, 17))
        self.assertIsNone(error)
        post.assert_not_called()
        holiday = lambda rows: rows[1].__setitem__(
            [i for i, v in enumerate(rows[0]) if v == (date(2026, 10, 15) - date(1899, 12, 30)).days][0], 'HL')
        sheets, post, error = self.run_job('verify', rows_edit=holiday)
        self.assertIsNone(error)
        post.assert_not_called()

    def test_verify_needs_only_the_alert_url(self):
        with patch.object(digest, 'Sheets') as sheets_cls:
            env = {'RUN_MODE': 'verify'}
            clock = Mock()
            clock.now.return_value.date.return_value = date(2026, 10, 15)
            with patch.dict(os.environ, env, clear=True), patch.object(digest, 'datetime', clock), \
                 self.assertRaises(DigestError):
                digest.run()
            sheets_cls.assert_not_called()

    def test_holidays_push_the_window_into_next_month_and_writes_follow(self):
        def week_off(rows):  # Oct 26-30 are holidays: the 10th working day moves from Oct 29 to Nov 5
            for d in range(26, 31):
                rows[1][rows[0].index((date(2026, 10, d) - date(1899, 12, 30)).days)] = 'HL'
        leave = Leave('U123', date(2026, 11, 4), date(2026, 11, 4), 'SL')
        sheets, post, error = self.run_job('live', now=date(2026, 10, 15), rows_edit=week_off, leaves=[leave])
        self.assertIsNone(error)
        self.assertIn('November 2026', sheets.reads)
        self.assertEqual(self.spans(post), ['Fri 16 Oct – Thu 22 Oct', 'Fri 23 Oct – Thu 5 Nov'])
        self.assertEqual([w['range'].split('!')[0] for w in sheets.write.call_args.args[0]], ["'November 2026'"])

    def test_write_window_still_reaches_month_end_mid_month(self):
        leave = Leave('U123', date(2026, 10, 30), date(2026, 10, 30), 'SL')  # after the 10th working day (Oct 29)
        sheets, post, error = self.run_job('live', now=date(2026, 10, 15), leaves=[leave])
        self.assertIsNone(error)
        self.assertEqual(len(sheets.write.call_args.args[0]), 1)

    def test_window_crosses_year_end_into_january(self):
        jan = cal_rows(2027, 1)
        jan[1][jan[0].index((date(2027, 1, 1) - date(1899, 12, 30)).days)] = 'HL'
        sheets, post, error = self.run_job('live', now=date(2026, 12, 28), titles=('December 2026', 'January 2027'),
                                           tabs={'December 2026': cal_rows(2026, 12), 'January 2027': jan})
        self.assertIsNone(error)
        self.assertIn('January 2027', sheets.reads)
        self.assertEqual(self.spans(post), ['Tue 29 Dec – Tue 5 Jan', 'Wed 6 Jan – Tue 12 Jan'])
        self.assertIn('Friday, January 1, 2027', str(post.call_args_list[-1].kwargs['json']))

    def test_preview_file_gets_the_text_rendering(self):
        path = os.path.join(os.environ.get('TMPDIR', '/tmp'), 'digest_preview_test.txt')
        if os.path.exists(path):
            os.remove(path)
        sheets, post, error = self.run_job('preview', env_extra={'PRIVATE_PREVIEW_PATH': path})
        self.assertIsNone(error)
        with open(path) as f:
            text = f.read()
        os.remove(path)
        self.assertTrue(text.startswith('Leaves: 15th October 2026'))
        self.assertIn('1. Sample Employee: Sick/Emergency leave', text)
        self.assertIn('Upcoming Leaves · next 5 working days (Fri 16 Oct – Thu 22 Oct):', text)
        self.assertIn('Following 5 working days (Fri 23 Oct – Thu 29 Oct):', text)

    def test_leave_spanning_month_end_is_written_into_both_tabs(self):
        leave = Leave('U123', date(2026, 10, 30), date(2026, 11, 3), 'SL')
        sheets, post, error = self.run_job('live', now=date(2026, 10, 26), leaves=[leave])
        self.assertIsNone(error)
        ranges = [w['range'] for w in sheets.write.call_args.args[0]]
        self.assertEqual(sorted(r.split('!')[0] for r in ranges),
                         ["'November 2026'", "'November 2026'", "'October 2026'"])  # Oct 30, Nov 2, Nov 3 (weekends skipped)

    def test_heads_up_not_sent_early_in_month_or_when_tab_exists(self):
        for kwargs in ({'now': date(2026, 10, 15), 'titles': ('October 2026',)}, {'now': date(2026, 10, 26)}):
            sheets, post, error = self.run_job('live', **kwargs)
            self.assertEqual(self.posted(post), ['leadership'])

    def test_mapping_names_missing_from_calendar_are_counted_not_named(self):
        mapping = [['Slack User ID', 'Tracker Name', 'Role', 'Team'], ['U123', 'Sample Employee', 'QA', 'V1'],
                   ['U9', 'Ghost Person', 'Shadow QA', 'V1']]
        with patch('builtins.print') as out:
            self.run_job('live', mapping_extra=mapping)
        line = next(c.args[0] for c in out.call_args_list if 'not found in the October 2026 calendar' in c.args[0])
        self.assertIn('1 Employee Mapping name(s)', line)
        self.assertIn('1 marked Shadow', line)
        self.assertNotIn('Ghost', line)

    def test_same_alert_and_leadership_url_is_warned_about(self):
        with patch('builtins.print') as out:
            self.run_job('live', env_extra={'ALERT_WEBHOOK_URL': 'https://example.invalid/leadership'})
        self.assertTrue(any('ALERT_WEBHOOK_URL equals TEAMS_WEBHOOK_URL' in c.args[0] for c in out.call_args_list))

    def test_applicants_outside_the_team_are_ignored_not_fatal(self):
        day = date(2026, 10, 15)
        leaves = [Leave('U123', day, day, 'SL'), Leave('U777', day, day, 'SL'), Leave('U555', day, day, 'HD')]
        with patch('builtins.print') as out:
            sheets, post, error = self.run_job('live', leaves=leaves)
        self.assertIsNone(error)
        (writes,) = sheets.write.call_args.args
        self.assertEqual(len(writes), 1)  # only the mapped employee's cell
        self.assertTrue(any('2 Slack applicant(s)' in str(c.args) for c in out.call_args_list))
        self.assertEqual(self.posted(post), ['leadership'])

    def test_preview_never_alerts(self):
        _, post, error = self.run_job('preview', rows_edit=self.pl_today)
        self.assertIsInstance(error, DigestError)
        post.assert_not_called()

    def test_pending_or_hand_typed_state_alerts_instead_of_exiting_quietly(self):
        for status in ('pending', 'sent', 'PENDING'):
            sheets, post, error = self.run_job('live', status=status)
            self.assertEqual(error.kind, 'delivery')
            sheets.write.assert_not_called()
            self.assertEqual(self.posted(post), ['alert'])

    def test_oversized_card_stops_before_any_write(self):
        with patch.object(digest, 'MAX_CARD_BYTES', 100):
            sheets, post, error = self.run_job('live')
        self.assertIsInstance(error, DigestError)
        sheets.write.assert_not_called()
        self.assertEqual(self.posted(post), ['alert'])

    def test_skips_say_why_and_how_to_resend(self):
        with patch('builtins.print') as out:
            self.run_job('live', status='accepted')
        self.assertIn('SKIPPED: today\'s digest was already sent', out.call_args.args[0])
        self.assertIn('delete today\'s row in the Digest State tab', out.call_args.args[0])
        self.assertIn('Nothing was written', out.call_args.args[0])
        with patch('builtins.print') as out:
            self.run_job('live', now=date(2026, 10, 17))
        self.assertIn('SKIPPED: today is a weekend', out.call_args.args[0])

    def test_skip_adds_actions_notice_and_job_summary(self):
        summary = os.path.join(os.environ.get('TMPDIR', '/tmp'), 'gh_summary_test')
        open(summary, 'w').close()
        with patch.dict(os.environ, {'GITHUB_ACTIONS': 'true', 'GITHUB_STEP_SUMMARY': summary}), \
             patch('builtins.print') as out:
            digest.skipped('test reason')
        self.assertTrue(any(c.args[0].startswith('::notice title=Digest skipped::SKIPPED: test reason')
                            for c in out.call_args_list))
        with open(summary) as f:
            self.assertIn('### Digest skipped\nSKIPPED: test reason', f.read())
        os.remove(summary)

    def test_weekend_skip_is_quiet_even_in_live_mode(self):
        sheets, post, error = self.run_job('live', now=date(2026, 10, 17))  # Saturday
        self.assertIsNone(error)
        sheets.write.assert_not_called()
        post.assert_not_called()

    def test_company_holiday_skip_is_quiet(self):
        sheets, post, error = self.run_job('live', rows_edit=lambda rows: rows[1].__setitem__(
            [i for i, v in enumerate(rows[0]) if v == (date(2026, 10, 15) - date(1899, 12, 30)).days][0], 'HL'))
        self.assertIsNone(error)
        sheets.write.assert_not_called()
        post.assert_not_called()

    def test_unexpected_error_alerts_generically_and_marks_output(self):
        post = Mock(return_value=SimpleNamespace(status_code=202))
        out = os.path.join(os.environ.get('TMPDIR', '/tmp'), 'gh_output_test')
        open(out, 'w').close()
        env = {'RUN_MODE': 'live', 'ALERT_WEBHOOK_URL': 'https://example.invalid/alert', 'GITHUB_OUTPUT': out,
               'GITHUB_SERVER_URL': 'https://github.example', 'GITHUB_REPOSITORY': 'o/r', 'GITHUB_RUN_ID': '7'}
        with patch.dict(os.environ, env, clear=True), patch.object(digest, 'run', side_effect=RuntimeError('secret-token')), \
             patch.dict(sys.modules, {'requests': SimpleNamespace(post=post)}), self.assertRaises(RuntimeError):
            digest.main()
        card = post.call_args.kwargs['json']['attachments'][0]['content']
        self.assertNotIn('secret-token', str(card))
        self.assertEqual(card['actions'][0]['url'], 'https://github.example/o/r/actions/runs/7')
        with open(out) as f:
            self.assertEqual(f.read(), 'alerted=true\n')
        os.remove(out)

    def test_failing_alert_never_masks_original_error(self):
        _, post, error = self.run_job('live', changed=True, alert_status=500)
        self.assertEqual(error.kind, 'stale')

    def test_live_requires_alert_url_and_bot_id_before_any_write(self):
        for key in ('ALERT_WEBHOOK_URL', 'SLACK_LEAVE_BOT_ID'):
            with patch.object(digest, 'Sheets') as sheets_cls:
                env = {'RUN_MODE': 'live', 'TEAMS_WEBHOOK_URL': 'https://example.invalid/leadership',
                       'ALERT_WEBHOOK_URL': 'https://example.invalid/alert', 'SLACK_LEAVE_BOT_ID': 'B1'}
                del env[key]
                clock = Mock()
                clock.now.return_value.date.return_value = date(2026, 10, 15)  # a Thursday, whatever today is
                with patch.dict(os.environ, env, clear=True), patch.object(digest, 'datetime', clock), \
                     self.assertRaises(DigestError):
                    digest.run()
                sheets_cls.assert_not_called()


class WindowAndCardTests(unittest.TestCase):
    """Two windows of 5 working days from tomorrow; team-grouped tables on the card."""

    @staticmethod
    def nov(marks=(), extra=()):
        """November 2026 tab; marks are (row, day, code), extra adds more people."""
        rows = cal_rows(2026, 11)
        rows += [[''] * 2 + [name] + [''] * 30 for name in extra]
        for r, day, code in marks:
            rows[r][rows[0].index((date(2026, 11, day) - date(1899, 12, 30)).days)] = code
        return Month('November 2026', rows, 2026, 11)

    def windows(self, today, marks=()):
        return [days for days, _ in make_digest([self.nov(marks)], today)['windows']]

    def assert_valid_card(self, card):
        content = card['attachments'][0]['content']
        self.assertEqual(content['version'], '1.5')
        elements = list(walk(content['body']))
        ids = [e['id'] for e in elements if 'id' in e]
        self.assertEqual(len(ids), len(set(ids)))
        for e in elements:
            if e.get('type') == 'Table':
                self.assertEqual(e['columns'], [{'width': '36px'}, {'width': 3}, {'width': 3}])
                self.assertTrue(e['firstRowAsHeader'] and e['showGridLines'])
                self.assertEqual(e['rows'][0]['style'], 'emphasis')
                for row in e['rows']:
                    self.assertEqual(len(row['cells']), len(e['columns']))
            if e.get('type') == 'Action.ToggleVisibility':
                self.assertTrue(set(e['targetElements']) <= set(ids))
        self.assertLessEqual(len(json.dumps(card)), digest.MAX_CARD_BYTES)
        return content['body']

    def test_windows_start_tomorrow_and_hold_five_working_days_each(self):
        cases = {date(2026, 11, 2): ['Tue 3 Nov – Mon 9 Nov', 'Tue 10 Nov – Mon 16 Nov'],    # Monday
                 date(2026, 11, 3): ['Wed 4 Nov – Tue 10 Nov', 'Wed 11 Nov – Tue 17 Nov'],   # Tuesday
                 date(2026, 11, 5): ['Fri 6 Nov – Thu 12 Nov', 'Fri 13 Nov – Thu 19 Nov'],   # Thursday
                 date(2026, 11, 6): ['Mon 9 Nov – Fri 13 Nov', 'Mon 16 Nov – Fri 20 Nov']}   # Friday: starts Monday
        for today, labels in cases.items():
            windows = self.windows(today)
            self.assertEqual([digest.span(w, w[-1]) for w in windows], labels)
            self.assertEqual([len(w) for w in windows], [5, 5])
            self.assertTrue(all(d.weekday() < 5 for w in windows for d in w))

    def test_holidays_extend_the_window_and_are_listed(self):
        tue = date(2026, 11, 3)
        nov = lambda *days: [date(2026, 11, d) for d in days]
        self.assertEqual(self.windows(tue, [(1, 9, 'HL')]), [nov(4, 5, 6, 10, 11), nov(12, 13, 16, 17, 18)])  # in window 1
        self.assertEqual(self.windows(tue, [(1, 13, 'HL')]), [nov(4, 5, 6, 9, 10), nov(11, 12, 16, 17, 18)])  # in window 2
        self.assertEqual(self.windows(tue, [(1, 4, 'HL')]), [nov(5, 6, 9, 10, 11), nov(12, 13, 16, 17, 18)])  # tomorrow
        d = make_digest([self.nov([(1, 3, 'HL'), (1, 4, 'HL'), (1, 18, 'HL'), (1, 25, 'HL')])], tue)
        # today is not "upcoming"; with Nov 4 and 18 off the 10th working day is Nov 19, so Nov 25 is past it
        self.assertEqual(d['holidays'], ['Wednesday, November 4, 2026', 'Wednesday, November 18, 2026'])

    def test_person_in_both_windows_shows_each_windows_dates(self):
        m = self.nov([(1, d, 'PL') for d in (9, 10, 11, 12)])
        (_, first), (_, second) = make_digest([m], date(2026, 11, 3))['windows']
        self.assertEqual(first, [('Sample Employee', '', '9th-10th Nov')])
        self.assertEqual(second, [('Sample Employee', '', '11th-12th Nov')])

    def test_only_planned_leave_on_window_working_days_is_upcoming(self):
        # Fri 6 counts; Sat 7 (weekend) is not a working day; Thu 19 is the 11th working day (windows end Wed 18)
        # because Tue 10 is a company holiday (every row HL).
        m = self.nov([(1, d, 'PL') for d in (6, 7, 19)] + [(r, 10, 'HL') for r in (1, 2)], extra=['Second Person'])
        d = make_digest([m], date(2026, 11, 3))
        (_, first), (_, second) = d['windows']
        self.assertEqual(first, [('Sample Employee', '', '6th Nov')])
        self.assertEqual(second, [])
        self.assertEqual(d['holidays'], ['Tuesday, November 10, 2026'])
        for code in ('SL', 'HD', 'CPL', 'ML', 'WK'):
            (_, first), _ = make_digest([self.nov([(1, 4, code)])], date(2026, 11, 3))['windows']
            self.assertEqual(first, [], code)

    def test_one_persons_hl_is_not_a_company_holiday(self):
        # Only one of two people has HL on Mon 9: the day stays a working day and the other's leave is listed.
        m = self.nov([(1, 9, 'PL'), (2, 9, 'HL')], extra=['Regional Holiday'])
        d = make_digest([m], date(2026, 11, 3))
        days, first = d['windows'][0]
        self.assertIn(date(2026, 11, 9), days)
        self.assertEqual(first, [('Sample Employee', '', '9th Nov')])
        self.assertEqual(d['holidays'], [])

    def test_shadow_rows_do_not_block_a_company_holiday(self):
        # A hidden (Shadow) row without HL must not stop a day every visible person has off from being a holiday.
        m = self.nov([(1, 10, 'HL')], extra=['Shadow Lead'])
        self.assertEqual(make_digest([m], date(2026, 11, 3), hidden={'shadow lead'})['holidays'],
                         ['Tuesday, November 10, 2026'])

    def test_toggle_hides_the_following_window_table(self):
        m = self.nov([(1, 4, 'PL'), (1, 12, 'PL')])
        body = self.assert_valid_card(digest_card(make_digest([m], date(2026, 11, 3))))
        toggle = next(e for e in body if 'selectAction' in e)
        self.assertEqual(toggle['selectAction']['targetElements'], ['following-table', 'following-show', 'following-hide'])
        labels = {e['id']: e for e in walk(toggle) if 'id' in e}
        self.assertNotIn('isVisible', labels['following-show'])
        self.assertIs(labels['following-hide']['isVisible'], False)
        self.assertIn('Following 5 working days (1)', [e.get('text') for e in walk(toggle)])
        hidden = body[body.index(toggle) + 1]
        self.assertEqual((hidden['type'], hidden['id'], hidden['isVisible']), ('Table', 'following-table', False))
        self.assertEqual(cell_texts(hidden)[0], ['#', 'Name', 'Dates'])
        self.assertIs(body[-1], hidden)
        self.assertEqual(body[-5]['text'], 'Upcoming Leaves · next 5 working days (1)')
        self.assertEqual((body[-4]['text'], body[-4]['spacing'], body[-4]['isSubtle']), ('Wed 4 Nov – Tue 10 Nov', 'None', True))
        self.assertEqual(body[-3]['type'], 'Table')

    def test_empty_windows_say_none_without_a_toggle(self):
        body = self.assert_valid_card(digest_card(make_digest([self.nov()], date(2026, 11, 3))))
        texts = [e.get('text') for e in body]
        self.assertEqual(texts[-6:], ['Upcoming Leaves · next 5 working days', 'Wed 4 Nov – Tue 10 Nov', 'None',
                                      'Following 5 working days', 'Wed 11 Nov – Tue 17 Nov', 'None'])
        self.assertFalse(any('selectAction' in e or 'id' in e for e in walk(body)))
        # window 1 empty, window 2 not: None, then the toggle
        body = self.assert_valid_card(digest_card(make_digest([self.nov([(1, 12, 'PL')])], date(2026, 11, 3))))
        self.assertEqual([e.get('text') for e in body][-5:-2], ['Upcoming Leaves · next 5 working days', 'Wed 4 Nov – Tue 10 Nov', 'None'])
        self.assertIn('selectAction', body[-2])

    def test_tables_group_by_team_with_continuous_numbering(self):
        rows = [('Ann', 'B', 'x'), ('Bob', 'A', 'x'), ('Cid', '', 'x'), ('Dee', 'A', 'x'), ('Eve', 'B', 'x'),
                ('Fay', 'C', 'x'), ('Gus', 'B', 'x')]
        d = {'title': 'Leaves: 3rd November 2026', 'holidays': [], 'sections': [('Sick/Emergency Leaves', rows)],
             'windows': [([], []), ([], [])], 'end': date(2026, 11, 30)}
        body = self.assert_valid_card(digest_card(d))
        self.assertEqual(body[1]['text'], 'Upcoming Holidays')
        self.assertEqual(body[3]['text'], 'Sick/Emergency Leaves (7)')
        table = body[4]
        self.assertEqual(cell_texts(table), [
            ['#', 'Name', 'Details'],
            ['', 'B (3)', ''], ['1', 'Ann', 'x'], ['2', 'Eve', 'x'], ['3', 'Gus', 'x'],
            ['', 'A (2)', ''], ['4', 'Bob', 'x'], ['5', 'Dee', 'x'],
            ['', 'C (1)', ''], ['6', 'Fay', 'x'],
            ['', 'No team (1)', ''], ['7', 'Cid', 'x']])
        self.assertEqual([r.get('style') for r in table['rows']][:3], ['emphasis', 'accent', None])

    def test_names_in_card_cells_have_no_team_suffix(self):
        m = self.nov([(1, 3, 'SL'), (1, 4, 'PL')])
        body = self.assert_valid_card(digest_card(make_digest([m], date(2026, 11, 3), teams={'sample employee': 'INT'})))
        names = [row[1] for e in body if e.get('type') == 'Table' for row in cell_texts(e)[2:]]
        self.assertEqual(names, ['Sample Employee', 'Sample Employee'])

    def test_worst_case_card_fits_and_says_how_many_more(self):
        def people(n, detail):
            return [(f'Firstname Middlename Lastname {i:03}', f'Team {i:03} Platform Engineering', detail) for i in range(n)]
        long_dates = '4th Nov, 6th Nov, 9th-10th Nov, 12th Nov'
        d = {'title': 'Leaves: 3rd November 2026',
             'holidays': [f'Wednesday, November {i}, 2026' for i in range(4, 18)],
             'sections': [(t, people(70, 'Half day (type unspecified in tracker)')) for t in
                          ('Sick/Emergency Leaves', 'Half-day Leaves', 'Planned Leaves', 'Other Entries')],
             'windows': [([date(2026, 11, i) for i in (4, 5, 6, 9, 10)], people(70, long_dates)),
                         ([date(2026, 11, i) for i in (11, 12, 13, 16, 17)], people(70, long_dates))], 'end': date(2026, 11, 17)}
        body = self.assert_valid_card(digest_card(d))
        tables = [e for e in body if e.get('type') == 'Table']
        self.assertEqual(len(tables), 6)
        for table in tables:
            shown = sum(1 for row in cell_texts(table) if row[0].isdigit())
            self.assertEqual(cell_texts(table)[-1], ['', f'...and {70 - shown} more', ''])
        self.assertIn('Sick/Emergency Leaves (70)', [e.get('text') for e in body])
        # a normal day is not capped
        d['sections'] = [('Sick/Emergency Leaves', people(25, 'Sick/Emergency leave'))]
        d['windows'] = [([], []), ([], [])]
        table = next(e for e in self.assert_valid_card(digest_card(d)) if e.get('type') == 'Table')
        self.assertEqual(cell_texts(table)[-1][0], '25')


class RetryTests(unittest.TestCase):
    def test_retrying_recovers_from_transient_errors_but_not_client_errors(self):
        ok, busy, bad = (SimpleNamespace(status_code=c) for c in (200, 503, 404))
        with patch.object(digest.time, 'sleep') as sleep:
            self.assertIs(digest.retrying(Mock(side_effect=[busy, ok])), ok)
            self.assertIs(digest.retrying(Mock(side_effect=[OSError(), busy, ok])), ok)
            self.assertEqual([c.args[0] for c in sleep.call_args_list], [2, 2, 4])
            self.assertIs(digest.retrying(Mock(side_effect=[bad])), bad)  # a 4xx is not retried
            self.assertIs(digest.retrying(Mock(side_effect=[busy, busy, busy])), busy)  # gives up after 3 tries
            with self.assertRaises(OSError):
                digest.retrying(Mock(side_effect=OSError()))

    def test_sheets_request_retries_then_reports_unreachable(self):
        sheets = digest.Sheets.__new__(digest.Sheets)
        sheets.base = 'https://example.invalid'
        sheets.http = Mock()
        sheets.http.request.side_effect = [SimpleNamespace(status_code=500, ok=False),
                                           SimpleNamespace(status_code=200, ok=True, json=lambda: {'ok': 1})]
        with patch.object(digest.time, 'sleep'):
            self.assertEqual(sheets.request('GET', '/x'), {'ok': 1})
            sheets.http.request.side_effect = OSError('boom')
            with self.assertRaises(DigestError) as ctx:
                sheets.request('GET', '/x')
        self.assertNotIn('boom', str(ctx.exception))

    def test_slack_unreachable_is_a_clean_error(self):
        env = {'SLACK_CHANNEL_ID': 'C1', 'SLACK_BOT_TOKEN': 'x', 'SLACK_LEAVE_BOT_ID': 'B'}
        get = Mock(side_effect=OSError('secret detail'))
        with patch.dict(os.environ, env, clear=True), patch.object(digest.time, 'sleep'), \
             patch.dict(sys.modules, {'requests': SimpleNamespace(get=get)}), self.assertRaises(DigestError) as ctx:
            digest.slack_requests(date(2026, 10, 15), date(2026, 10, 31))
        self.assertEqual(get.call_count, 3)
        self.assertNotIn('secret detail', str(ctx.exception))


class SlackAndStateTests(unittest.TestCase):
    TEXT = '<@U123> applied a Unplanned - Sick/Emergency Leave from 2026-10-15 to 2026-10-15. Full Day'

    def history(self, pages, bot='BSOURCE'):
        get = Mock(side_effect=[SimpleNamespace(ok=True, json=Mock(return_value=p)) for p in pages])
        env = {'SLACK_CHANNEL_ID': 'C1', 'SLACK_BOT_TOKEN': 'xoxb-test', 'SLACK_LEAVE_BOT_ID': bot}
        with patch.dict(os.environ, env, clear=True), patch.dict(sys.modules, {'requests': SimpleNamespace(get=get)}):
            return digest.slack_requests(date(2026, 10, 15), date(2026, 10, 31))

    def test_only_the_configured_bot_is_trusted(self):
        other = {'bot_id': 'BOTHER', 'text': 'Is the leave approved? Approved'}  # a different workflow: ignored
        page = {'ok': True, 'messages': [other, {'bot_id': 'BSOURCE', 'text': self.TEXT}]}
        self.assertEqual(len(self.history([page])), 1)

    def test_template_request_from_another_bot_is_a_config_error_not_silence(self):
        page = {'ok': True, 'messages': [{'bot_id': 'BNEW', 'ts': '1.2', 'text': self.TEXT}]}
        with self.assertRaises(DigestError) as ctx:
            self.history([page])
        self.assertEqual(ctx.exception.kind, 'config')
        self.assertIn('SLACK_LEAVE_BOT_ID', str(ctx.exception))
        self.assertEqual(ctx.exception.detail, 'Slack message ts 1.2, bot BNEW')

    def test_pagination_follows_cursor_and_rejects_incomplete_history(self):
        msg = {'bot_id': 'BSOURCE', 'text': self.TEXT}
        pages = [{'ok': True, 'messages': [msg], 'response_metadata': {'next_cursor': 'c2'}}, {'ok': True, 'messages': [msg]}]
        self.assertEqual(len(self.history(pages)), 2)
        with self.assertRaises(DigestError):
            self.history([{'ok': True, 'messages': [msg], 'has_more': True}])

    def test_denied_history_and_ended_leave_handling(self):
        with self.assertRaises(DigestError):
            self.history([{'ok': False}])
        old = self.TEXT.replace('2026-10-15 to 2026-10-15', '2026-10-01 to 2026-10-02')
        self.assertEqual(self.history([{'ok': True, 'messages': [{'bot_id': 'BSOURCE', 'text': old}]}]), [])

    def test_unrecognised_bot_messages_are_counted_not_dropped_silently(self):
        page = {'ok': True, 'messages': [{'bot_id': 'BSOURCE', 'text': 'a reworded message'}, {'bot_id': 'BSOURCE', 'text': ''}]}
        with patch('builtins.print') as out:
            self.assertEqual(self.history([page]), [])
        self.assertIn('2 bot messages', out.call_args.args[0])

    def sheets_with(self, rows):
        sheets = digest.Sheets.__new__(digest.Sheets)
        sheets.titles = {'Digest State'}
        sheets.read = Mock(return_value=rows)
        return sheets

    def test_state_lookup_and_guards(self):
        today = date(2026, 10, 15)
        self.assertEqual(self.sheets_with([['Date', 'Status'], ['2026-10-14', 'accepted'], ['2026-10-15', 'pending']]).state(today), ('pending', 3))
        self.assertEqual(self.sheets_with([['Date', 'Status'], ['2026-10-14', 'accepted']]).state(today), (None, 3))
        self.assertEqual(self.sheets_with([]).state(today), (None, 2))
        with self.assertRaises(DigestError):
            self.sheets_with([['Wrong', 'Headers']]).state(today)
        with self.assertRaises(DigestError):
            self.sheets_with([['Date', 'Status']] + [['2026-01-01', 'accepted']] * 5000).state(today)


if __name__ == '__main__':
    unittest.main()
