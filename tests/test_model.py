import unittest

from server.model import LLMModel, ModelConfig


class ModelTests(unittest.TestCase):
    def test_stable_across_order_and_instances(self):
        model = LLMModel(ModelConfig(seed=123))
        first = {name: model.plan(name, 100, 100) for name in ("a", "b", "c")}
        second = {name: LLMModel(ModelConfig(seed=123)).plan(name, 100, 100)
                  for name in ("c", "b", "a")}
        self.assertEqual(first, second)
        self.assertEqual(first["a"], model.plan("a", 100, 100))

    def test_output_bounds_and_client_cap(self):
        model = LLMModel(ModelConfig(output_min=20, output_max=30))
        samples = [model.plan(str(i), 0, 100).output_tokens for i in range(100)]
        self.assertTrue(all(20 <= size <= 30 for size in samples))
        self.assertGreater(len(set(samples)), 1)
        self.assertEqual(model.plan("a", 0, 7).output_tokens, 7)

    def test_delay_formula_and_partial_tokens(self):
        plan = LLMModel(ModelConfig(output_min=4, output_max=4, base_ttft=0.1,
                                   input_token_seconds=0.01,
                                   output_token_seconds=0.05)).plan("task", 10, 100)
        self.assertAlmostEqual(plan.prefill_seconds, 0.2)
        self.assertAlmostEqual(plan.decode_seconds, 0.15)
        self.assertAlmostEqual(plan.total_seconds, 0.35)
        self.assertEqual(plan.generated_tokens(0.199), 0)
        self.assertEqual(plan.generated_tokens(plan.prefill_seconds), 1)
        self.assertEqual(plan.generated_tokens(plan.prefill_seconds + 0.05), 2)
        self.assertEqual(plan.generated_tokens(plan.prefill_seconds + 0.10), 3)
        self.assertEqual(plan.generated_tokens(plan.total_seconds), 4)
        self.assertEqual(plan.generated_tokens(100), 4)

    def test_zero_interval_and_one_token(self):
        model = LLMModel(ModelConfig(output_min=4, output_max=4, base_ttft=0.1,
                                    input_token_seconds=0, output_token_seconds=0))
        plan = model.plan("task", 0, 100)
        self.assertEqual(plan.generated_tokens(0.099), 0)
        self.assertEqual(plan.generated_tokens(0.1), 4)
        one = model.plan("task", 0, 1)
        self.assertEqual(one.decode_seconds, 0)
        self.assertEqual(one.generated_tokens(0.1), 1)

    def test_invalid_model_configuration(self):
        for args in [{"seed": True}, {"seed": -1}, {"seed": 2**32},
                     {"output_min": 0}, {"output_max": 1_000_001},
                     {"output_min": 50, "output_max": 10},
                     {"base_ttft": float("nan")}, {"input_token_seconds": float("inf")},
                     {"output_token_seconds": -0.1}, {"output_token_seconds": True}]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                ModelConfig(**args)

    def test_invalid_workload_and_duration(self):
        model = LLMModel(ModelConfig())
        for name, inputs, maximum in [("", 1, 1), (" " * 3, 1, 1), ("x" * 129, 1, 1),
                                      ("a", -1, 1), ("a", True, 1),
                                      ("a", 1, 0), ("a", 1, 1_000_001)]:
            with self.subTest(values=(name, inputs, maximum)), self.assertRaises(ValueError):
                model.plan(name, inputs, maximum)
        with self.assertRaises(ValueError):
            model.plan("too-long", 1_000_000_000, 100)
        plan = model.plan("ok", 1, 1)
        for elapsed in [-1, True, float("inf"), float("nan")]:
            with self.subTest(elapsed=elapsed), self.assertRaises(ValueError):
                plan.generated_tokens(elapsed)
