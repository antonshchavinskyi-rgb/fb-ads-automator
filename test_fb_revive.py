import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import fb_revive_recent as recent
import fb_revive_deep as deep


class ReviveSplitTests(unittest.TestCase):
    def test_recent_time_range(self):
        ranges = recent.build_time_ranges(
            datetime(2026, 8, 14, 5, 45, tzinfo=ZoneInfo('Europe/Warsaw'))
        )
        self.assertEqual(ranges['recent_label'], '2026-08-12–2026-08-13')
        self.assertEqual(set(ranges), {'recent', 'recent_label'})

    def test_deep_time_ranges(self):
        ranges = deep.build_time_ranges(
            datetime(2026, 8, 14, 5, 45, tzinfo=ZoneInfo('Europe/Warsaw'))
        )
        self.assertEqual(ranges['idle_label'], '2026-08-07–2026-08-13')
        self.assertEqual(ranges['history_label'], '2026-07-24–2026-08-06')
        self.assertEqual(set(ranges), {'idle', 'history', 'idle_label', 'history_label'})

    def test_recent_thresholds(self):
        base = {'be': 10.0, 'recent_leads': 2, 'recent_spend': 19.98}
        self.assertTrue(recent.qualifies_recent(base)[0])
        self.assertFalse(recent.qualifies_recent({**base, 'recent_spend': 20.0})[0])
        one = {'be': 10.0, 'recent_leads': 1, 'recent_spend': 7.0}
        self.assertTrue(recent.qualifies_recent(one)[0])
        self.assertFalse(recent.qualifies_recent({**one, 'recent_spend': 7.01})[0])

    def test_deep_thresholds_and_idle_guard(self):
        base = {'be': 10.0, 'hist_leads': 2, 'hist_spend': 16.0, 'idle_impressions': 0}
        self.assertTrue(deep.qualifies_deep(base)[0])
        self.assertFalse(deep.qualifies_deep({**base, 'hist_spend': 16.01})[0])
        self.assertFalse(deep.qualifies_deep({**base, 'idle_impressions': 1})[0])
        one = {'be': 10.0, 'hist_leads': 1, 'hist_spend': 6.0, 'idle_impressions': 0}
        self.assertTrue(deep.qualifies_deep(one)[0])

    def test_logic_is_physically_separated(self):
        self.assertNotIn('qualifies_deep', recent.__dict__)
        self.assertNotIn('qualifies_recent', deep.__dict__)

    def test_deep_fails_closed_when_idle_insights_fail(self):
        ranges = deep.build_time_ranges(
            datetime(2026, 8, 14, 5, 45, tzinfo=ZoneInfo('Europe/Warsaw'))
        )

        def fake_fetch(endpoint, params, errors=None, context=''):
            if context.startswith('campaigns'):
                return [{'id': 'c1', 'name': '1391 - зал', 'effective_status': 'ACTIVE'}]
            if context.startswith('adsets'):
                return [{'id': 'a1', 'name': 'A1', 'status': 'PAUSED', 'campaign_id': 'c1'}]
            if context.startswith('history insights'):
                return [{
                    'adset_id': 'a1',
                    'spend': '10',
                    'impressions': '20',
                    'actions': [{'action_type': 'lead', 'value': '2'}],
                }]
            if context.startswith('idle insights'):
                errors.append('simulated idle API failure')
                return []
            return []

        errors = []
        with patch.object(deep, 'fetch_data', side_effect=fake_fetch):
            candidates, _ = deep.get_account_state('acc', 'USD', ranges, errors, [])

        self.assertEqual(candidates, {})
        self.assertEqual(errors, ['simulated idle API failure'])


if __name__ == '__main__':
    unittest.main()
