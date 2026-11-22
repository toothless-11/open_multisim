"""Parameter-space candidate generation for coarse and fine searches."""

import itertools
import math
import random


def coarse_candidates(initial, lower, upper):
    """Build sorted per-resistor coarse grids, including initial and upper values."""
    candidates = {}
    for ref, initial_value in initial.items():
        low = float(lower[ref])
        high = float(upper[ref])
        initial_value = float(initial_value)
        if not all(math.isfinite(value) for value in (low, high, initial_value)):
            raise ValueError("{} requires finite resistance values".format(ref))
        if not (0.0 < low < high):
            raise ValueError("{} requires positive lower < upper bounds".format(ref))
        if not low <= initial_value <= high:
            raise ValueError(
                "initial value for {} ({}) is outside [{}, {}]".format(
                    ref, initial_value, low, high
                )
            )
        step = max(20.0, 0.05 * initial_value)
        values = [low + step * index for index in range(int((high - low) / step) + 1)]
        values.extend((initial_value, high))
        candidates[ref] = sorted(set(round(value, 12) for value in values if value <= high))
    return candidates


def _product_dicts(refs, value_lists):
    for values in itertools.product(*value_lists):
        yield dict(zip(refs, values))


def generate_coarse_combinations(candidates, budget, seed=None, initial=None):
    """Enumerate or Latin-hypercube sample a discrete multidimensional grid."""
    if not isinstance(budget, int) or isinstance(budget, bool) or budget < 0:
        raise ValueError("budget must be a non-negative integer")
    refs = sorted(candidates)
    if not refs or budget == 0:
        return []
    value_lists = [list(candidates[ref]) for ref in refs]
    if any(not values for values in value_lists):
        raise ValueError("each parameter requires at least one candidate")
    total = math.prod(len(values) for values in value_lists)
    if total <= budget:
        return list(_product_dicts(refs, value_lists))

    rng = random.Random(seed)
    count = min(int(budget), total)
    selected = []
    seen = set()

    def add(indices):
        key = tuple(indices)
        if key not in seen and len(selected) < count:
            seen.add(key)
            selected.append(dict(
                (ref, value_lists[dimension][index])
                for dimension, (ref, index) in enumerate(zip(refs, key))
            ))

    if initial:
        initial_indices = []
        for ref, values in zip(refs, value_lists):
            nearest = min(range(len(values)), key=lambda i: abs(values[i] - initial[ref]))
            initial_indices.append(nearest)
        add(tuple(initial_indices))
    else:
        add(tuple(len(values) // 2 for values in value_lists))
    add(tuple(0 for _ in refs))
    add(tuple(len(values) - 1 for values in value_lists))

    strata = []
    for values in value_lists:
        n = len(values)
        bins = [min(n - 1, int((i + rng.random()) * n / count)) for i in range(count)]
        rng.shuffle(bins)
        strata.append(bins)
    for row in range(count):
        add(tuple(strata[column][row] for column in range(len(refs))))

    # Collisions are possible after snapping strata to a discrete grid.
    attempts = 0
    while len(selected) < count and attempts < count * 20:
        add(tuple(rng.randrange(len(values)) for values in value_lists))
        attempts += 1
    if len(selected) < count:
        for indices in itertools.product(*(range(len(values)) for values in value_lists)):
            add(indices)
            if len(selected) == count:
                break
    return selected


def generate_fine_candidates(center, lower, upper, coarse_steps):
    """Create per-parameter fine grids in the clipped +/- two-coarse-step window."""
    candidates = {}
    for ref, center_value in center.items():
        center_value = float(center_value)
        low = float(lower[ref])
        high = float(upper[ref])
        coarse_step = float(coarse_steps[ref])
        if not all(math.isfinite(value) for value in (low, high, center_value, coarse_step)):
            raise ValueError("fine-search values for {} must be finite".format(ref))
        if not (0.0 < low < high) or coarse_step <= 0.0:
            raise ValueError("invalid fine-search bounds or step for {}".format(ref))
        fine_step = max(20.0, coarse_step / 5.0)
        window_low = max(low, float(center_value) - 2.0 * coarse_step)
        window_high = min(high, float(center_value) + 2.0 * coarse_step)
        values = [
            window_low + index * fine_step
            for index in range(int((window_high - window_low) / fine_step) + 1)
        ]
        values.extend((window_low, window_high, float(center_value)))
        candidates[ref] = sorted(set(
            round(value, 12)
            for value in values
            if window_low <= value <= window_high
        ))
    return candidates


def generate_fine_combinations(candidates):
    """Yield fine-search combinations in deterministic lexicographic order."""
    refs = sorted(candidates)
    if not refs:
        return
    for combination in _product_dicts(refs, [candidates[ref] for ref in refs]):
        yield combination

