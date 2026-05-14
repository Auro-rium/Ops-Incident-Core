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
    max_words = int(max_tokens / 1.3)
    overlap_words = int(overlap_tokens / 1.3)

    if len(words) <= max_words:
        return [text]

    chunks: list[str] = []
    start = 0
    while start < len(words):
        end = start + max_words
        chunk_words = words[start:end]
        chunks.append(" ".join(chunk_words))
        start = end - overlap_words

    return chunks
