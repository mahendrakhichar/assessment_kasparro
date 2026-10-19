"""
test_validate.py — unit tests for offset location and claim validation.
"""
import pytest
from agent.validate import locate_quote, validate_claims, split_into_sentences, Claim


class TestLocateQuote:
    def test_simple_find(self):
        text = "Hello world. Goodbye world."
        assert locate_quote(text, "Hello world") == (0, 11)

    def test_not_found(self):
        assert locate_quote("Hello world", "goodbye") is None  # case-sensitive

    def test_cursor_advances_for_duplicates(self):
        text = "A. A. A."
        # First occurrence
        assert locate_quote(text, "A.", 0) == (0, 2)
        # Starting after first
        assert locate_quote(text, "A.", 1) == (3, 5)

    def test_no_whitespace_normalization(self):
        text = "Hello  world"  # double space
        assert locate_quote(text, "Hello  world") == (0, 12)
        assert locate_quote(text, "Hello world") is None  # single space not found

    def test_empty_quote_not_found(self):
        # empty string is found at position 0 in any string, which is degenerate
        # We expect callers to check for empty quote before calling
        result = locate_quote("abc", "")
        assert result == (0, 0)


class TestValidateClaims:
    AI_ANSWER = "The phone has a 5000 mAh battery. The phone costs $299. The phone costs $299."

    def test_valid_claim_v1(self):
        raw = [{"quote": "The phone has a 5000 mAh battery.", "label": "supported", "fact_ids": [], "reason": "test"}]
        claims, errors = validate_claims("A1", self.AI_ANSWER, raw, version="v1")
        assert len(claims) == 1
        assert errors == []
        assert claims[0].start_char == 0
        assert claims[0].end_char == 33

    def test_quote_not_found_verbatim(self):
        raw = [{"quote": "The phone has 5000 mAh battery", "label": "supported", "fact_ids": [], "reason": "x"}]
        claims, errors = validate_claims("A1", self.AI_ANSWER, raw, version="v1")
        assert len(claims) == 0
        assert any(e.code == "quote_not_found" for e in errors)

    def test_invalid_label(self):
        raw = [{"quote": "The phone costs $299.", "label": "maybe", "fact_ids": [], "reason": "x"}]
        claims, errors = validate_claims("A1", self.AI_ANSWER, raw, version="v1")
        assert len(claims) == 0
        assert any(e.code == "invalid_label" for e in errors)

    def test_overlapping_spans_rejected(self):
        raw = [
            {"quote": "The phone costs $299.", "label": "supported", "fact_ids": [], "reason": "x"},
            {"quote": "phone costs $299.", "label": "supported", "fact_ids": [], "reason": "x"},
        ]
        claims, errors = validate_claims("A1", self.AI_ANSWER, raw, version="v1")
        # Second overlaps first
        assert any(e.code == "overlapping_span" for e in errors)

    def test_duplicate_sentence_cursor_advances(self):
        """Two identical sentences should be located at their respective positions."""
        raw = [
            {"quote": "The phone costs $299.", "label": "supported", "fact_ids": [], "reason": "x"},
            {"quote": "The phone costs $299.", "label": "contradicted", "fact_ids": [], "reason": "y"},
        ]
        claims, errors = validate_claims("A1", self.AI_ANSWER, raw, version="v1")
        assert len(claims) == 2
        assert claims[0].start_char != claims[1].start_char

    def test_v2_sentence_id_lookup(self):
        sentences = [
            ("First sentence.", 0, 15),
            ("Second sentence.", 16, 32),
        ]
        raw = [{"sentence_id": "S0", "label": "supported", "fact_ids": [], "reason": "test"}]
        claims, errors = validate_claims("A1", "First sentence. Second sentence.", raw,
                                        version="v2", sentences=sentences)
        assert len(claims) == 1
        assert claims[0].quote == "First sentence."

    def test_v2_invalid_sentence_id(self):
        sentences = [("Only.", 0, 5)]
        raw = [{"sentence_id": "S99", "label": "supported", "fact_ids": [], "reason": "test"}]
        claims, errors = validate_claims("A1", "Only.", raw, version="v2", sentences=sentences)
        assert any(e.code == "sentence_id_out_of_range" for e in errors)


class TestSplitSentences:
    def test_basic_split(self):
        text = "Hello world. This is great. Goodbye."
        sents = split_into_sentences(text)
        assert len(sents) >= 2

    def test_offsets_are_correct(self):
        text = "First sentence. Second sentence."
        sents = split_into_sentences(text)
        for sent_text, start, end in sents:
            assert text[start:end] == sent_text
