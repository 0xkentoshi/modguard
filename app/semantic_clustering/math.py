import math


def cosine_similarity(
    left: list[float],
    right: list[float],
) -> float:
    if (
        not left
        or not right
        or len(left) != len(right)
    ):
        return 0.0

    dot = sum(
        a * b
        for a, b in zip(left, right)
    )
    left_norm = math.sqrt(
        sum(value * value for value in left)
    )
    right_norm = math.sqrt(
        sum(value * value for value in right)
    )

    if left_norm <= 0.0 or right_norm <= 0.0:
        return 0.0

    return max(
        -1.0,
        min(
            1.0,
            float(dot / (left_norm * right_norm)),
        ),
    )
