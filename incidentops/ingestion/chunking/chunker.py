"""
Chunker — token counting and chunk size management.
"""

from __future__ import annotations


def count_tokens(text: str) -> int:
    """
    Approximate token count. Uses whitespace splitting as a fast heuristic.
    ~1.3 tokens per whitespace-delimited word for English text.
    """
    words = len(text.split())
    return int(words * 1.3)


def split_text_by_tokens(text: str, max_tokens: int = 512, overlap_tokens: int = 50) -> list[str]:
    """
    Split text into chunks of roughly max_tokens, with overlap.
    Used as a fallback when a section is too large.
    """
    words = text.split()
    # Rough conversion: 1.3 tokens per word
    max_words = max(1, int(max_tokens / 1.3))
    # Overlap must stay below the window size or the cursor never advances.
    overlap_words = min(max(0, int(overlap_tokens / 1.3)), max_words - 1)

    if len(words) <= max_words:
        return [text]

    chunks: list[str] = []
    start = 0
    while start < len(words):
        end = start + max_words
        chunks.append(" ".join(words[start:end]))
        if end >= len(words):
            break
        next_start = end - overlap_words
        assert next_start > start, "split_text_by_tokens must advance"
        start = next_start

    return chunks
