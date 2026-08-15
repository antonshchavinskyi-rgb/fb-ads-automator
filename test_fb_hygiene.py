import unittest
from unittest.mock import patch

import fb_hygiene as hygiene


OLD_TIME = '2026-01-01T00:00:00+0000'


class HygieneFailClosedTests(unittest.TestCase):
    def test_adset_insights_error_never_pauses_adset(self):
        def fake_fetch(endpoint, params, errors=None, context=''):
            if context.startswith('adsets account'):
                return [{
                    'id': 'a1',
                    'name': 'Old AdSet',
                    'effective_status': 'ACTIVE',
                    'created_time': OLD_TIME,
                    'campaign_id': 'c1',
                    'campaign': {'name': '1391 - зал'},
                }]
            if context.startswith('adset insights'):
                errors.append('simulated adset insights failure')
                return []
            if context.startswith('ads account'):
                return []
            return []

        events, errors = [], []
        with patch.object(hygiene, 'fetch_data', side_effect=fake_fetch), \
             patch.object(hygiene, 'change_entity_status') as change_status:
            hygiene.process_hygiene_logic('acc', events, errors)

        change_status.assert_not_called()
        self.assertEqual(events, [])
        self.assertEqual(errors, ['simulated adset insights failure'])

    def test_successful_empty_adset_insights_still_means_zero_impressions(self):
        def fake_fetch(endpoint, params, errors=None, context=''):
            if context.startswith('adsets account'):
                return [{
                    'id': 'a1',
                    'name': 'Old AdSet',
                    'effective_status': 'ACTIVE',
                    'created_time': OLD_TIME,
                    'campaign_id': 'c1',
                    'campaign': {'name': '1391 - зал'},
                }]
            if context.startswith('adset insights'):
                return []
            if context.startswith('ads account'):
                return []
            return []

        events, errors = [], []
        with patch.object(hygiene, 'fetch_data', side_effect=fake_fetch), \
             patch.object(hygiene, 'change_entity_status', return_value=True) as change_status:
            hygiene.process_hygiene_logic('acc', events, errors)

        change_status.assert_called_once_with('a1', 'PAUSED')
        self.assertEqual(len(events), 1)
        self.assertEqual(errors, [])

    def test_ad_insights_error_never_pauses_ad(self):
        def fake_fetch(endpoint, params, errors=None, context=''):
            if context.startswith('adsets account'):
                return []
            if context.startswith('ads account'):
                return [{
                    'id': 'ad1',
                    'name': 'Old Ad',
                    'effective_status': 'ACTIVE',
                    'created_time': OLD_TIME,
                    'adset_id': 'a1',
                }]
            if context.startswith('ad insights'):
                errors.append('simulated ad insights failure')
                return []
            return []

        events, errors = [], []
        with patch.object(hygiene, 'fetch_data', side_effect=fake_fetch), \
             patch.object(hygiene, 'change_entity_status') as change_status:
            hygiene.process_hygiene_logic('acc', events, errors)

        change_status.assert_not_called()
        self.assertEqual(events, [])
        self.assertEqual(errors, ['simulated ad insights failure'])


if __name__ == '__main__':
    unittest.main()

