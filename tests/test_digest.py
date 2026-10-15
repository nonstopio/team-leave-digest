import unittest
import copy
import os
import sys
from types import SimpleNamespace
from unittest.mock import Mock, patch
from datetime import date, timedelta
import digest
from digest import DigestError, Leave, Month, column, date_list, employee_mapping, build_card, digest_card, make_digest, parse_request, webhook_url


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
        self.assertIn('19th Oct, 21st Oct', text)
        self.assertNotIn('22 Oct', text)
        self.assertIn('Tuesday, October 20, 2026', text)
        self.assertIn('Leaves: 15th October 2026', text)

    def test_utilities_and_card(self):
        self.assertEqual(column(34), 'AH')
        self.assertEqual(date_list([date(2026, 10, 19), date(2026, 10, 20), date(2026, 10, 30)]), '19-20th Oct, 30th Oct')
        self.assertEqual(date_list([date(2026, 10, 19)]), '19th Oct')
        self.assertEqual(date_list([date(2026, 10, 30), date(2026, 10, 31), date(2026, 11, 1)]), '30th Oct-1st Nov')
        self.assertEqual([digest.ordinal(n) for n in (1, 2, 3, 4, 11, 12, 13, 21, 22, 23)],
                         ['1st', '2nd', '3rd', '4th', '11th', '12th', '13th', '21st', '22nd', '23rd'])
        self.assertEqual(build_card('T', 'good', 'Sample')['attachments'][0]['content']['type'], 'AdaptiveCard')

    def test_card_keeps_raw_text_and_separate_lines(self):
        m, today = month(), date(2026, 10, 15)
        m.rows[1][m.name_col] = "O'Brien & Sons"
        m.rows[1][m.dates[today]] = 'SL'
        m.rows[1][m.dates[date(2026, 10, 19)]] = 'PL'
        m.rows[1][m.dates[date(2026, 10, 20)]] = 'PL'
        m = Month(m.title, m.rows, 2026, 10)
        card = digest_card(make_digest([m], today, date(2026, 10, 31)))
        content = card['attachments'][0]['content']
        self.assertTrue(content['$schema'].startswith('http'))
        # leadership digest is plain text like the previously hand-posted message: no coloured header
        self.assertEqual(content['body'][0], {'type': 'TextBlock', 'text': 'Leaves: 15th October 2026', 'wrap': True})
        self.assertNotIn('Container', [b['type'] for b in content['body']])
        self.assertNotIn('actions', content)
        texts = [b['text'] for b in content['body'] if b['type'] == 'TextBlock']
        self.assertTrue(any("O'Brien & Sons: Sick/Emergency leave" in t for t in texts))
        self.assertFalse(any('&amp;' in t or '&#x27;' in t for t in texts))
        # one paragraph per item: a single newline would collapse in Teams markdown
        self.assertTrue(any(t.startswith('Sick/Emergency Leaves:\n\n1.') for t in texts))
        self.assertTrue(any(t == 'Upcoming Leaves:\n\n1. O\'Brien & Sons: 19-20th Oct' for t in texts))
        self.assertGreater(len(texts), 3)

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
        text = make_digest([m], today, today)
        self.assertIn('Other Entries', text)
        self.assertIn('Sample Employee: ML', text)
        self.assertNotIn('Other Entries', make_digest([month()], today, today))

    def test_item_cap(self):
        m, today = month(), date(2026, 10, 15)
        m.rows.extend([[''] * m.name_col + [f'Person {i}'] + [''] * 31 for i in range(60)])
        for r in range(1, len(m.rows)):
            m.rows[r][m.dates[today]] = 'SL'
        m = Month(m.title, m.rows, 2026, 10)
        self.assertIn('...and 11 more', make_digest([m], today, today))

    def test_webhook_url_rejects_unsafe_values(self):
        for bad in ['http://example.invalid/x', 'https://u:p@example.invalid/x',
                    'https://outlook.office.com/webhook/abc', 'https://contoso.webhook.office.com/x']:
            with patch.dict(os.environ, {'HOOK': bad}), self.assertRaises(DigestError):
                webhook_url('HOOK')
        with patch.dict(os.environ, {'HOOK': 'https://prod.logic.azure.com/x'}):
            self.assertEqual(webhook_url('HOOK'), 'https://prod.logic.azure.com/x')


class MainTests(unittest.TestCase):
    def run_job(self, mode='preview', status=None, post_status=202, changed=False, leaves=None, post_error=None,
                alert_status=202, env_extra=None, post_statuses=None, claim_error_on=None, rows_edit=None, now=date(2026, 10, 15)):
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

    def test_transient_5xx_is_retried_once_without_failing(self):
        sheets, post, error = self.run_job('live', post_statuses=[503, 202])
        self.assertIsNone(error)
        self.assertEqual(self.posted(post), ['leadership', 'leadership'])
        self.assertEqual(sheets.claim.call_args_list[-1].args[-1], 'accepted')

    def test_persistent_5xx_marks_failed_after_one_retry(self):
        sheets, post, error = self.run_job('live', post_statuses=[500, 500])
        self.assertIsNotNone(error)
        self.assertEqual(self.posted(post), ['leadership', 'leadership', 'alert'])
        self.assertEqual(sheets.claim.call_args_list[-1].args[-1], 'failed')

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

    def test_conflict_alerts_owners_only(self):
        sl = Leave('U123', date(2026, 10, 15), date(2026, 10, 15), 'SL')
        sheets, post, error = self.run_job('live', leaves=[sl, Leave('U999', sl.start, sl.end, 'SL')])
        self.assertEqual(error.kind, 'conflict')
        sheets.write.assert_not_called()
        self.assertEqual(self.posted(post), ['alert'])
        body = str(post.call_args.kwargs['json'])
        self.assertIn('NOT sent', body)
        self.assertIn('Slack user U999', body)  # details reach the owners' channel only
        self.assertNotIn('U999', str(error))    # ...never the public message/log line

    def test_preview_never_alerts(self):
        _, post, error = self.run_job('preview', leaves=[Leave('U999', date(2026, 10, 15), date(2026, 10, 15), 'SL')])
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


class SlackAndStateTests(unittest.TestCase):
    TEXT = '<@U123> applied a Unplanned - Sick/Emergency Leave from 2026-10-15 to 2026-10-15. Full Day'

    def history(self, pages, bot='BSOURCE'):
        get = Mock(side_effect=[SimpleNamespace(ok=True, json=Mock(return_value=p)) for p in pages])
        env = {'SLACK_CHANNEL_ID': 'C1', 'SLACK_BOT_TOKEN': 'xoxb-test', 'SLACK_LEAVE_BOT_ID': bot}
        with patch.dict(os.environ, env, clear=True), patch.dict(sys.modules, {'requests': SimpleNamespace(get=get)}):
            return digest.slack_requests(date(2026, 10, 15), date(2026, 10, 31))

    def test_only_the_configured_bot_is_trusted(self):
        page = {'ok': True, 'messages': [{'bot_id': 'BOTHER', 'text': self.TEXT}, {'bot_id': 'BSOURCE', 'text': self.TEXT}]}
        self.assertEqual(len(self.history([page])), 1)

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
