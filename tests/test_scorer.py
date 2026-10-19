"""
test_scorer.py — unit tests for the scorer module.

Critical test: G=10, P=10, M=8, C=7 → ext_f1=0.80, joint_f1=0.70 (per spec).
"""
import pytest
from harness.scorer import score, Metrics, LABELS


def make_gold(n: int, label: str = "supported") -> list[dict]:
    return [
        {"answer_id": "A1", "start_char": i * 10, "end_char": i * 10 + 5, "label": label}
        for i in range(n)
    ]


def make_pred(n: int, label: str = "supported", start_offset: int = 0) -> list[dict]:
    return [
        {
            "answer_id": "A1",
            "start_char": i * 10 + start_offset,
            "end_char": i * 10 + 5 + start_offset,
            "label": label,
        }
        for i in range(n)
    ]


class TestSpecExample:
    """The canonical example from the spec: G=10, P=10, M=8, C=7."""

    def test_extraction_f1(self):
        # 8 matched out of G=10, P=10 → 2*8/(10+10) = 0.80
        gold = make_gold(10)
        # First 8 match gold exactly, last 2 are novel (unmatched)
        pred = make_gold(8) + make_pred(2, start_offset=500)
        m = score(gold, pred, set())
        assert m.G == 10
        assert m.P == 10
        assert m.M == 8
        assert abs(m.extraction_f1 - 0.80) < 1e-9

    def test_joint_f1(self):
        # C=7: 7 correctly labeled, 1 wrong label
        gold = make_gold(10)
        correct = make_gold(7)
        wrong = [{"answer_id": "A1", "start_char": 70, "end_char": 75, "label": "contradicted"}]  # wrong label
        extra = make_pred(2, start_offset=500)
        pred = correct + wrong + extra
        m = score(gold, pred, set())
        assert m.C == 7
        assert abs(m.joint_f1 - 0.70) < 1e-9


class TestEdgeCases:
    def test_empty_gold_empty_pred(self):
        m = score([], [], set())
        assert m.extraction_f1 == 0.0
        assert m.joint_f1 == 0.0
        assert m.macro_f1 == 0.0

    def test_error_answer_excluded_from_P(self):
        """Error answer predictions must not count in P, but gold stays in G."""
        gold = [{"answer_id": "A1", "start_char": 0, "end_char": 5, "label": "supported"}]
        pred = [{"answer_id": "A1", "start_char": 0, "end_char": 5, "label": "supported"}]
        m = score(gold, pred, error_answer_ids={"A1"})
        assert m.G == 1
        assert m.P == 0  # excluded because A1 errored
        assert m.M == 0
        assert m.extraction_f1 == 0.0

    def test_no_overlap_different_answers(self):
        gold = [
            {"answer_id": "A1", "start_char": 0, "end_char": 5, "label": "supported"},
            {"answer_id": "A2", "start_char": 0, "end_char": 5, "label": "contradicted"},
        ]
        pred = [
            {"answer_id": "A1", "start_char": 0, "end_char": 5, "label": "supported"},
            # A2 not predicted
        ]
        m = score(gold, pred, set())
        assert m.G == 2
        assert m.P == 1
        assert m.M == 1
        assert m.C == 1
        assert abs(m.extraction_f1 - 2 * 1 / 3) < 1e-9

    def test_confusion_matrix_shape(self):
        gold = make_gold(3, "supported")
        pred = make_gold(3, "contradicted")  # all wrong label
        m = score(gold, pred, set())
        # 3 matches, 0 correctly labeled
        assert m.M == 3
        assert m.C == 0
        # confusion: supported row, contradicted col should be 3
        assert m.confusion["supported"]["contradicted"] == 3

    def test_check_run_zero_preds_but_gold_in_G(self):
        gold = make_gold(5)
        m = score(gold, [], set())
        assert m.G == 5
        assert m.P == 0
        assert m.M == 0
        assert m.extraction_f1 == 0.0

    def test_perfect_score(self):
        gold = make_gold(5)
        pred = make_gold(5)
        m = score(gold, pred, set())
        assert m.extraction_f1 == 1.0
        assert m.joint_f1 == 1.0

    def test_per_label_f1_computation(self):
        gold = [
            {"answer_id": "A1", "start_char": 0, "end_char": 5, "label": "supported"},
            {"answer_id": "A1", "start_char": 10, "end_char": 15, "label": "contradicted"},
        ]
        pred = [
            {"answer_id": "A1", "start_char": 0, "end_char": 5, "label": "supported"},
            {"answer_id": "A1", "start_char": 10, "end_char": 15, "label": "contradicted"},
        ]
        m = score(gold, pred, set())
        assert m.per_label["supported"]["f1"] == 1.0
        assert m.per_label["contradicted"]["f1"] == 1.0
        assert m.per_label["unknown"]["f1"] == 0.0  # no unknowns
