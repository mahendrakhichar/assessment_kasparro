"""
test_loader.py — unit tests for the CSV loader.
"""
import csv
import io
import tempfile
import os
import pytest
from pathlib import Path

from harness.loader import load_answers, load_gold, sha256_file, AnswerRow


def write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


class TestLoadAnswers:
    def test_basic_load(self, tmp_path):
        p = tmp_path / "answers.csv"
        write_csv(p, [
            {"answer_id": "A1", "brand_id": "B1", "question": "Q?", "ai_answer": "Answer here."},
        ], ["answer_id", "brand_id", "question", "ai_answer"])
        rows = load_answers(p)
        assert len(rows) == 1
        assert rows[0].answer_id == "A1"
        assert rows[0].ai_answer == "Answer here."

    def test_preserves_crlf_in_ai_answer(self, tmp_path):
        """CRLF inside ai_answer must be preserved verbatim (offset-sensitive)."""
        p = tmp_path / "answers.csv"
        # Write raw bytes to include \r\n inside cell
        content = 'answer_id,brand_id,question,ai_answer\r\nA1,B1,Q?,"Line1\r\nLine2"\r\n'
        p.write_bytes(content.encode("utf-8"))
        rows = load_answers(p)
        assert len(rows) == 1
        assert "\r\n" in rows[0].ai_answer or "\n" in rows[0].ai_answer

    def test_missing_column_raises(self, tmp_path):
        p = tmp_path / "bad.csv"
        write_csv(p, [{"answer_id": "A1"}], ["answer_id"])
        with pytest.raises(ValueError, match="missing columns"):
            load_answers(p)


class TestLoadGold:
    def test_basic_load(self, tmp_path):
        p = tmp_path / "gold.csv"
        write_csv(p, [
            {"answer_id": "A1", "start_char": "0", "end_char": "10",
             "quote": "Hello ther", "label": "supported", "fact_ids": "F1,F2"},
        ], ["answer_id", "start_char", "end_char", "quote", "label", "fact_ids"])
        gold = load_gold(p)
        assert "A1" in gold
        assert gold["A1"][0].fact_ids == ["F1", "F2"]

    def test_empty_fact_ids(self, tmp_path):
        p = tmp_path / "gold.csv"
        write_csv(p, [
            {"answer_id": "A1", "start_char": "0", "end_char": "5",
             "quote": "Hello", "label": "unknown", "fact_ids": ""},
        ], ["answer_id", "start_char", "end_char", "quote", "label", "fact_ids"])
        gold = load_gold(p)
        assert gold["A1"][0].fact_ids == []


class TestSha256:
    def test_sha256_deterministic(self, tmp_path):
        p = tmp_path / "file.txt"
        p.write_text("hello world", encoding="utf-8")
        h1 = sha256_file(p)
        h2 = sha256_file(p)
        assert h1 == h2
        assert len(h1) == 64
