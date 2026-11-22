"""Write optimization results without modifying the source circuit file."""

import json
import os

from multisim_api import save_circuit_as, save_waveform_image


def _create_unique_directory(path):
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    base = os.path.basename(os.path.normpath(path))
    candidate = os.path.join(parent, base)
    suffix = 1
    while True:
        try:
            os.mkdir(candidate)
            return candidate
        except FileExistsError:
            candidate = os.path.join(parent, "{}_{:03d}".format(base, suffix))
            suffix += 1


def _json_safe_summary(summary):
    return {
        key: value
        for key, value in summary.items()
        if key != "best_waveform"
    }


def save_optimization_report(summary, output_directory, circuit=None):
    """Persist JSON/SVG reports and SaveAs only when a passing result exists."""
    result_directory = _create_unique_directory(output_directory)
    best = summary.get("best")
    waveform = summary.get("best_waveform")
    if best is not None and not waveform:
        raise ValueError("best result is missing its waveform data")
    if best is not None:
        save_waveform_image(
            waveform["times"],
            waveform["values"],
            path=os.path.join(result_directory, "best_waveform.svg"),
            title="Optimized output waveform",
            x_label="Time (s)",
            y_label="Voltage (V)",
            frequency=best["frequency"],
            time_range=(best["start_time"], best["end_time"]),
        )
        with open(
            os.path.join(result_directory, "best_parameters.json"),
            "w",
            encoding="utf-8",
        ) as file:
            json.dump(best, file, ensure_ascii=False, indent=2, allow_nan=False)
    with open(
        os.path.join(result_directory, "search_summary.json"),
        "w",
        encoding="utf-8",
    ) as file:
        json.dump(
            _json_safe_summary(summary),
            file,
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        )
    if best is not None and circuit is not None:
        circuit_path = os.path.join(result_directory, "circuit_optimized.ms14")
        if os.path.exists(circuit_path):
            raise FileExistsError(circuit_path)
        save_circuit_as(circuit, circuit_path)
    return result_directory

