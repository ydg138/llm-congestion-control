import unittest

from server.rate_limit import MultiQuota, TokenBucket


class QuotaTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        clock = lambda: self.now
        self.quota = MultiQuota(TokenBucket(3, 1, clock), TokenBucket(100, 10, clock),
                                TokenBucket(50, 5, clock))

    def test_reserve_all_dimensions(self):
        decision = self.quota.reserve(20, 30)
        self.assertTrue(decision.allowed)
        snapshots = self.quota.snapshot()
        self.assertEqual([snapshots[name]["remaining"] for name in snapshots], [2, 80, 20])

    def test_failed_output_reservation_does_not_debit_other_buckets(self):
        self.quota.reserve(20, 30)
        before = self.quota.snapshot()
        decision = self.quota.reserve(10, 30)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.limited_by, ["output_tokens"])
        self.assertEqual(decision.retry_after_seconds, 2)
        self.assertEqual(self.quota.snapshot(), before)

    def test_multiple_limits_report_longest_wait_and_recover(self):
        self.quota.reserve(100, 50)
        decision = self.quota.reserve(20, 20)
        self.assertEqual(decision.limited_by, ["input_tokens", "output_tokens"])
        self.assertEqual(decision.retry_after_seconds, 4)
        self.now = 4
        self.assertTrue(self.quota.reserve(20, 20).allowed)

    def test_impossible_cost_is_distinct_from_temporary_rate_limit(self):
        before = self.quota.snapshot()
        result = self.quota.reserve(101, 0)
        self.assertTrue(result.impossible)
        self.assertFalse(result.allowed)
        self.assertIsNone(result.retry_after_seconds)
        self.assertEqual(result.limited_by, ["input_tokens"])
        self.assertEqual(self.quota.snapshot(), before)

    def test_zero_cost_still_uses_one_request(self):
        self.assertTrue(self.quota.reserve(0, 0).allowed)
        self.assertEqual(self.quota.requests.snapshot()["remaining"], 2)
        self.assertEqual(self.quota.inputs.snapshot()["remaining"], 100)
        self.assertEqual(self.quota.outputs.snapshot()["remaining"], 50)

    def test_completion_refunds_unused_output_only(self):
        self.quota.reserve(20, 30)
        self.quota.settle_output(reserved=30, actual=12)
        self.assertEqual(self.quota.outputs.snapshot()["remaining"], 38)
        self.assertEqual(self.quota.inputs.snapshot()["remaining"], 80)
        self.assertEqual(self.quota.requests.snapshot()["remaining"], 2)

    def test_cancellation_refund_and_refill_never_exceed_capacity(self):
        self.quota.reserve(20, 30)
        self.now = 10
        self.quota.settle_output(reserved=30, actual=0)
        self.assertEqual(self.quota.outputs.snapshot()["remaining"], 50)

    def test_invalid_costs_and_settlement_leave_balances_intact(self):
        before = self.quota.snapshot()
        for value in [True, -1, 1.5, float("inf"), 10**10]:
            with self.assertRaises(ValueError):
                self.quota.reserve(value, 1)
            with self.assertRaises(ValueError):
                self.quota.reserve(1, value)
        with self.assertRaises(ValueError):
            self.quota.settle_output(10, 11)
        self.assertEqual(self.quota.snapshot(), before)

    def test_reconfiguration_charges_elapsed_time_at_old_rate(self):
        bucket = self.quota.inputs
        bucket.consume(100)
        self.now = 2
        bucket.reconfigure(refill_rate=20)
        self.assertEqual(bucket.snapshot()["remaining"], 20)
        self.now = 3
        self.assertEqual(bucket.snapshot()["remaining"], 40)

    def test_capacity_shrink_clips_and_growth_does_not_refill(self):
        bucket = self.quota.inputs
        bucket.reconfigure(capacity=30)
        self.assertEqual(bucket.snapshot()["remaining"], 30)
        bucket.reconfigure(capacity=100)
        self.assertEqual(bucket.snapshot()["remaining"], 30)

    def test_invalid_reconfiguration_is_not_partially_applied(self):
        before = self.quota.inputs.snapshot()
        with self.assertRaises(ValueError):
            self.quota.inputs.reconfigure(capacity=20, refill_rate=0)
        self.assertEqual(self.quota.inputs.snapshot(), before)

    def test_single_bucket_preview_does_not_debit_and_returns_none_for_impossible(self):
        bucket = self.quota.outputs
        self.assertTrue(bucket.preview(50).allowed)
        self.assertEqual(bucket.snapshot()["remaining"], 50)
        self.assertIsNone(bucket.preview(51).retry_after_seconds)
