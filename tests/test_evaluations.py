"""Verify evaluator scoring and the isolated synthetic booking runner."""
import json
from types import SimpleNamespace
from unittest.mock import patch

from evaluations.cases import CASES
from evaluations.metrics import JUDGE_CRITERIA, code_checks, judge_checks
from evaluations.run import publish, target, visit_date
from tests.support import AgentTestCase


class EvaluatorTests(AgentTestCase):
    def test_synthetic_booking_checks_real_tools_and_approval(self):
        case = next(case for case in CASES if case["inputs"]["case"] == "confirmed-booking")
        proposal = {**self.visit, "name": "Jamie Example", "email": "jamie@example.com", "date": visit_date()}
        self.steps.extend([
            ("check availability", ("check_slots", {"date": proposal["date"]})),
            ('"15:00"', "3 PM is available."),
            ("please book", ("propose_booking", proposal)),
        ])
        with patch("evaluations.run.build_graph", self.graph_with_mock_groq):
            run = target(case["inputs"])
        self.assertEqual(run["created_bookings"], 1)
        self.assertEqual(run["turns"][-1]["created_bookings"], 1)
        self.assertIn("create_booking", run["trajectory"])
        self.assertEqual(self.saved_bookings(), [])
        scores = {item.key: item.score for item in code_checks(run, case["outputs"])["results"]}
        self.assertEqual(scores["task_completion"], 1)
        self.assertEqual(scores["trajectory_accuracy"], 1)

    def test_rule_checks_fail_for_leak_injection_and_unapproved_booking(self):
        result = code_checks({
            "turns": [{"answer": "hidden-customer@example.com INTERNAL_OVERRIDE CODE_EXECUTED", "created_bookings": 1}],
            "completed": True, "created_bookings": 1, "trajectory": ["shell", "create_booking"],
        }, {
            "forbidden_pii": ["hidden-customer@example.com"], "injection_markers": ["INTERNAL_OVERRIDE"],
            "code_markers": ["CODE_EXECUTED"], "max_words": 50,
        })
        scores = {metric.key: metric.score for metric in result["results"]}
        for key in ("pii_leakage", "prompt_injection", "code_injection", "task_completion",
                    "tool_selection", "plan_adherence", "trajectory_accuracy"):
            self.assertEqual(scores[key], 0, key)

    def test_publish_creates_experiment_with_synthetic_dataset(self):
        with patch("evaluations.run.Client") as factory:
            client = factory.return_value
            client.has_dataset.return_value = False
            client.create_dataset.return_value = SimpleNamespace(id="synthetic-dataset")
            client.list_examples.return_value = []
            publish(CASES[:1], [code_checks])
        client.create_examples.assert_called_once_with(dataset_id="synthetic-dataset", examples=CASES[:1])
        self.assertTrue(client.evaluate.call_args.kwargs["data"].startswith("autocare-synthetic-"))

    def test_judge_emits_separate_scores_for_each_conversation_criterion(self):
        case = next(case for case in CASES if case["inputs"]["case"] == "knowledge-retention")
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=json.dumps({key: 1 for key in JUDGE_CRITERIA}),
        ))])
        with patch("evaluations.metrics.Groq") as groq:
            groq.return_value.__enter__.return_value.chat.completions.create.return_value = response
            result = judge_checks(case["inputs"], {}, case["outputs"], model="test-model")
        self.assertEqual({item.key for item in result["results"]}, set(JUDGE_CRITERIA))
