import pytest

from app.semantic_clustering.math import cosine_similarity


def test_semantic_identical_vectors_are_one():
    assert cosine_similarity(
        [1.0, 2.0, 3.0],
        [1.0, 2.0, 3.0],
    ) == pytest.approx(1.0)


def test_semantic_unrelated_vectors_are_zero():
    assert cosine_similarity(
        [1.0, 0.0],
        [0.0, 1.0],
    ) == pytest.approx(0.0)


def test_dimension_mismatch_never_matches():
    assert cosine_similarity(
        [1.0],
        [1.0, 0.0],
    ) == 0.0
