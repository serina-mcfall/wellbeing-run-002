"""Task graph validator (control/task_graph.py) — ported from the Run 002
apparatus/task-graph/validate.js test suite; same cases, same error codes."""

import unittest

from control import task_graph


class TestTaskGraphValidation(unittest.TestCase):
    def test_the_real_config_tasks_json_graph_is_valid(self):
        result = task_graph.validate_run002_task_graph()
        self.assertTrue(result["ok"])
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["task_count"], 9)

    def test_a_dangling_dependency_is_rejected(self):
        result = task_graph.validate_task_graph(
            tasks=[{"id": "A", "depends_on": ["DOES-NOT-EXIST"]}])
        self.assertFalse(result["ok"])
        self.assertTrue(any(e.startswith("DANGLING_DEPENDENCY") for e in result["errors"]))

    def test_a_self_dependency_is_rejected(self):
        result = task_graph.validate_task_graph(tasks=[{"id": "A", "depends_on": ["A"]}])
        self.assertFalse(result["ok"])
        self.assertTrue(any(e.startswith("SELF_DEPENDENCY") for e in result["errors"]))

    def test_a_two_node_cycle_is_rejected(self):
        result = task_graph.validate_task_graph(tasks=[
            {"id": "A", "depends_on": ["B"]},
            {"id": "B", "depends_on": ["A"]},
        ])
        self.assertFalse(result["ok"])
        self.assertTrue(any(e.startswith("CYCLE") for e in result["errors"]))

    def test_a_duplicate_task_id_is_rejected(self):
        result = task_graph.validate_task_graph(tasks=[
            {"id": "A", "depends_on": []},
            {"id": "A", "depends_on": []},
        ])
        self.assertFalse(result["ok"])
        self.assertTrue(any(e.startswith("DUPLICATE_TASK_ID") for e in result["errors"]))

    def test_a_valid_linear_chain_passes_with_no_errors(self):
        result = task_graph.validate_task_graph(tasks=[
            {"id": "A", "depends_on": []},
            {"id": "B", "depends_on": ["A"]},
            {"id": "C", "depends_on": ["A", "B"]},
        ])
        self.assertTrue(result["ok"])
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["task_count"], 3)

    def test_an_empty_task_graph_fails_closed(self):
        result = task_graph.validate_task_graph(tasks=[])
        self.assertFalse(result["ok"])
        self.assertTrue(any(e.startswith("EMPTY_TASK_GRAPH") for e in result["errors"]))

    def test_a_missing_depends_on_field_is_rejected(self):
        result = task_graph.validate_task_graph(tasks=[{"id": "A"}])
        self.assertFalse(result["ok"])
        self.assertTrue(any(e.startswith("MALFORMED_DEPENDENCIES") for e in result["errors"]))

    def test_a_non_list_depends_on_value_is_rejected(self):
        result = task_graph.validate_task_graph(
            tasks=[{"id": "A", "depends_on": "TASK-001"}])
        self.assertFalse(result["ok"])
        self.assertTrue(any(e.startswith("MALFORMED_DEPENDENCIES") for e in result["errors"]))


if __name__ == "__main__":
    unittest.main()
