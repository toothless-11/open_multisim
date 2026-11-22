"""Identify distinct high-performing regions among distortion-qualified samples."""

import math


def identify_regions(results, lower, upper, coarse_steps, limit=3):
    """Return Vpp-ranked representative samples, suppressing nearby candidates."""
    if not isinstance(limit, int) or limit <= 0:
        raise ValueError("limit must be a positive integer")
    passing = [
        item for item in results
        if item.get("status") == "PASS"
        and item.get("vpp") is not None
        and math.isfinite(float(item["vpp"]))
    ]
    passing.sort(
        key=lambda item: (-float(item["vpp"]), float(item.get("distortion", 0.0)))
    )
    widths = {}
    for ref in lower:
        low, high = float(lower[ref]), float(upper[ref])
        if not (math.isfinite(low) and math.isfinite(high) and low < high):
            raise ValueError("region bounds must be finite and have positive width")
        widths[ref] = high - low
    radius = math.sqrt(sum(
        (float(coarse_steps[ref]) / widths[ref]) ** 2 for ref in widths
    ))
    representatives = []
    for candidate in passing:
        point = candidate["parameters"]
        is_near = False
        for chosen in representatives:
            other = chosen["parameters"]
            distance = math.sqrt(sum(
                ((float(point[ref]) - float(other[ref])) / widths[ref]) ** 2
                for ref in widths
            ))
            if distance <= radius:
                is_near = True
                break
        if not is_near:
            representatives.append(candidate)
            if len(representatives) >= limit:
                break
    return representatives

