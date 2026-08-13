import unittest

import fb_scaler as scaler


class ScalerBusinessLogicTests(unittest.TestCase):
    def test_source_tiers_and_lead_caps(self):
        self.assertEqual(scaler.source_base_duplicates(0.60), 18)
        self.assertEqual(scaler.source_base_duplicates(0.60001), 15)
        self.assertEqual(scaler.source_base_duplicates(0.75), 15)
        self.assertEqual(scaler.source_base_duplicates(0.75001), 12)
        self.assertEqual(scaler.source_base_duplicates(0.90), 12)
        self.assertEqual(scaler.source_base_duplicates(0.90001), 0)

        self.assertEqual(scaler.source_lead_cap(1, 0.70), 6)
        self.assertEqual(scaler.source_lead_cap(1, 0.70001), 0)
        self.assertEqual(scaler.source_lead_cap(3, 0.90), 12)
        self.assertEqual(scaler.source_lead_cap(3, 0.90001), 0)

    def test_offer_tiers(self):
        self.assertEqual(scaler.offer_scale(0.90), (1.00, False))
        self.assertEqual(scaler.offer_scale(0.95), (0.75, False))
        self.assertEqual(scaler.offer_scale(1.00), (0.50, False))
        self.assertEqual(scaler.offer_scale(1.10), (0.50, True))
        self.assertEqual(scaler.offer_scale(1.10001), (0.00, False))

    def test_duplicate_calculation_cost_goal(self):
        result = scaler.calculate_requested_duplicates(
            source_leads=1,
            source_cpl_ratio=0.60,
            offer_cpl_ratio=0.90,
            bid_strategy="COST_CAP",
        )
        self.assertTrue(result["eligible"])
        self.assertEqual(result["requested"], 6)

    def test_duplicate_calculation_bid_cap_floors_once(self):
        result = scaler.calculate_requested_duplicates(
            source_leads=3,
            source_cpl_ratio=0.74,
            offer_cpl_ratio=0.94,
            bid_strategy="LOWEST_COST_WITH_BID_CAP",
        )
        # min(15, 12) × 0.50 × 0.75 = 4.5 -> conservative floor 4
        self.assertEqual(result["requested"], 4)

    def test_weak_source_never_rescued_by_offer(self):
        result = scaler.calculate_requested_duplicates(
            source_leads=8,
            source_cpl_ratio=0.91,
            offer_cpl_ratio=0.50,
            bid_strategy="COST_CAP",
        )
        self.assertFalse(result["eligible"])
        self.assertEqual(result["requested"], 0)

    def test_caps_prioritize_more_leads_then_lower_ratio(self):
        old_account_cap = scaler.SCALER_ACCOUNT_CAP
        old_campaign_cap = scaler.SCALER_CAMPAIGN_CAP
        scaler.SCALER_ACCOUNT_CAP = 10
        scaler.SCALER_CAMPAIGN_CAP = 8
        try:
            rows = [
                {
                    "account_id": "1",
                    "campaign_id": "A",
                    "source_adset_id": "101",
                    "source_leads": 5,
                    "source_cpl_ratio": 0.80,
                    "requested": 7,
                },
                {
                    "account_id": "1",
                    "campaign_id": "A",
                    "source_adset_id": "102",
                    "source_leads": 6,
                    "source_cpl_ratio": 0.85,
                    "requested": 6,
                },
                {
                    "account_id": "1",
                    "campaign_id": "B",
                    "source_adset_id": "103",
                    "source_leads": 2,
                    "source_cpl_ratio": 0.50,
                    "requested": 6,
                },
            ]
            allocated = scaler.allocate_caps(rows)
            by_id = {row["source_adset_id"]: row for row in allocated}
            self.assertEqual(by_id["102"]["allocated"], 6)
            self.assertEqual(by_id["101"]["allocated"], 2)
            self.assertEqual(by_id["103"]["allocated"], 2)
        finally:
            scaler.SCALER_ACCOUNT_CAP = old_account_cap
            scaler.SCALER_CAMPAIGN_CAP = old_campaign_cap

    def test_name_marker_is_corrected_only_on_new_name(self):
        source = "antenna2911 / фб моб кос — Копия"
        corrected = scaler.corrected_source_name(
            source,
            "LOWEST_COST_WITH_BID_CAP",
        )
        self.assertIn("бід", corrected)
        self.assertNotIn("кос", corrected)
        self.assertNotIn("Копия", corrected)

    def test_marker_and_sequence_parser(self):
        marker = scaler.scaler_marker("2026-08-13", "1200", 2)
        self.assertEqual(
            marker,
            "[SCALER:2026-08-13:SRC-1200:N-02]",
        )
        sequences = scaler.parse_marker_sequences(
            [f"name {marker}", "other"],
            "2026-08-13",
            "1200",
        )
        self.assertEqual(sequences, {2})

    def test_start_time_and_jitter_are_deterministic(self):
        first = scaler.deterministic_start_time("2026-08-13", "1200", 1)
        second = scaler.deterministic_start_time("2026-08-13", "1200", 1)
        self.assertEqual(first, second)
        self.assertEqual(first.date().isoformat(), "2026-08-14")
        self.assertEqual(first.hour, 5)
        self.assertGreaterEqual(first.minute, 35)
        self.assertLessEqual(first.minute, 55)

        bid_one = scaler.deterministic_jittered_bid(
            10000,
            "2026-08-13",
            "1200",
            1,
        )
        bid_two = scaler.deterministic_jittered_bid(
            10000,
            "2026-08-13",
            "1200",
            1,
        )
        self.assertEqual(bid_one, bid_two)
        jittered, fraction = bid_one
        self.assertNotEqual(jittered, 10000)
        self.assertGreaterEqual(abs(fraction), 0.005)
        self.assertLessEqual(abs(fraction), 0.010)

    def test_build_plan_aggregates_offer_across_currencies(self):
        snapshots = [
            {
                "account_id": "usd",
                "currency": "USD",
                "rate": 1.0,
                "adsets": [{
                    "id": "100",
                    "name": "creative / фб моб кос",
                    "account_id": "usd",
                    "campaign_id": "10",
                    "status": "ACTIVE",
                    "effective_status": "ACTIVE",
                    "bid_strategy": "COST_CAP",
                    "bid_amount": "1100",
                    "daily_budget": "2200",
                    "campaign": {
                        "id": "10",
                        "name": "1389 - зал - offer",
                        "status": "ACTIVE",
                        "effective_status": "ACTIVE",
                    },
                }],
                "stats": {
                    "100": {"spend": 9.0, "leads": 2, "impressions": 1000},
                },
            },
            {
                "account_id": "pln",
                "currency": "PLN",
                "rate": 3.8,
                "adsets": [{
                    "id": "200",
                    "name": "other / фб моб кос",
                    "account_id": "pln",
                    "campaign_id": "20",
                    "status": "PAUSED",
                    "effective_status": "PAUSED",
                    "bid_strategy": "COST_CAP",
                    "bid_amount": "2900",
                    "daily_budget": "5800",
                    "campaign": {
                        "id": "20",
                        "name": "1389 - зал - offer",
                        "status": "ACTIVE",
                        "effective_status": "ACTIVE",
                    },
                }],
                "stats": {
                    "200": {"spend": 34.2, "leads": 2, "impressions": 1000},
                },
            },
        ]
        plan = scaler.build_plan(snapshots, "2026-08-13")
        offer = plan["offer_totals"]["1389"]
        self.assertAlmostEqual(offer["spend_usd"], 18.0)
        self.assertEqual(offer["leads"], 4)
        self.assertAlmostEqual(offer["cpl_ratio"], 0.5)
        self.assertEqual(len(plan["candidates"]), 2)
        self.assertEqual(plan["planned_new_duplicates"], 24)

    def test_catalog_is_excluded_from_scaler(self):
        snapshot = {
            "account_id": "1",
            "currency": "USD",
            "rate": 1.0,
            "adsets": [{
                "id": "300",
                "name": "catalog creative / кос",
                "campaign_id": "30",
                "status": "ACTIVE",
                "bid_strategy": "COST_CAP",
                "campaign": {
                    "id": "30",
                    "name": "бро - ктг - всі товари - каталог",
                    "status": "ACTIVE",
                },
            }],
            "stats": {
                "300": {"spend": 1.0, "leads": 10, "impressions": 1000},
            },
        }
        plan = scaler.build_plan([snapshot], "2026-08-13")
        self.assertEqual(plan["candidates"], [])
        self.assertEqual(plan["planned_new_duplicates"], 0)
        self.assertEqual(plan["skipped"][0]["reason"], "catalog campaign excluded")


if __name__ == "__main__":
    unittest.main()
