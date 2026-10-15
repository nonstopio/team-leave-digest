import unittest
import copy
import os
import sys
from types import SimpleNamespace
from unittest.mock import Mock, patch
from datetime import date, timedelta
import digest
from digest import DigestError, Leave, Month, column, date_list, employee_mapping, make_digest, parse_request, teams_payload


def month(name_col=2):
    start = date(2026, 10, 1)
    headers = [''] * name_col + ['Name'] + [(start + timedelta(days=i) - date(1899, 12, 30)).days for i in range(31)]
    return Month('October 2026', [headers, [''] * name_col + ['Sample Employee'] + [''] * 31], 2026, 10)


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
        text = make_digest([m], today, date(2026, 10, 31))
        self.assertIn('Sick/Emergency leave', text)
        self.assertIn('19 Oct, 21 Oct', text)
        self.assertNotIn('22 Oct', text)
        self.assertIn('Tuesday, 20 October 2026', text)

    def test_utilities_and_card(self):
        self.assertEqual(column(34), 'AH')
        self.assertEqual(date_list([date(2026, 10, 19), date(2026, 10, 20)]), '19 Oct–20 Oct')
        self.assertEqual(teams_payload('Sample')['attachments'][0]['content']['type'], 'AdaptiveCard')


class MainTests(unittest.TestCase):
    def run_job(self, mode='preview', status=None, post_status=202, changed=False):
        rows = month().rows
        sheets = Mock()
        mapping_rows = [['Slack User ID', 'Tracker Name'], ['U123', 'Sample Employee']]
        sheets.read.side_effect = lambda title, area: copy.deepcopy(mapping_rows if title == 'Employee Mapping' else rows)
        sheets.state.return_value = (status, 2)
        if changed:
            modified = copy.deepcopy(rows)
            modified[1][2] = 'Renamed Employee'
            sheets.read.side_effect = [mapping_rows, copy.deepcopy(rows), modified]
        clock = Mock()
        clock.now.return_value.date.return_value = date(2026, 10, 15)
        request = Mock(return_value=SimpleNamespace(status_code=post_status))
        env = {'RUN_MODE': mode,
               'TEAMS_WEBHOOK_URL': 'https://example.invalid/webhook'}
        leave = Leave('U123', date(2026, 10, 15), date(2026, 10, 15), 'SL')
        with patch.dict(os.environ, env, clear=True), patch.object(digest, 'datetime', clock), \
             patch.object(digest, 'Sheets', return_value=sheets), \
             patch.object(digest, 'slack_requests', return_value=[leave]), \
             patch.dict(sys.modules, {'requests': SimpleNamespace(post=request)}):
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

    def test_existing_pending_blocks_resend(self):
        sheets, post, error = self.run_job('live', status='pending')
        self.assertIsNone(error)
        sheets.write.assert_not_called()
        post.assert_not_called()

    def test_failed_delivery_stays_pending(self):
        sheets, post, error = self.run_job('live', post_status=500)
        self.assertIsInstance(error, DigestError)
        sheets.claim.assert_called_once_with(date(2026, 10, 15), 2, 'pending')

    def test_intervening_roster_edit_blocks_write_and_post(self):
        sheets, post, error = self.run_job('live', changed=True)
        self.assertIsInstance(error, DigestError)
        sheets.write.assert_not_called()
        post.assert_not_called()


if __name__ == '__main__':
    unittest.main()
