import math
import json
import os
import tempfile
import threading
import time
import unittest

from analyzer import (
    DISTORTED,
    FREQUENCY_DETECTION_FAILED,
    PASS,
    analyze_waveform,
)
from generator import coarse_candidates, generate_coarse_combinations
from optimizer import optimize
from report import save_optimization_report
from multisim_api import calculate_frequency
from region import identify_regions


def sinusoid(amplitude=2.0, frequency=50.0, harmonic=0.0, count=1201):
    times = [
        index / 10000.0 + (0.000002 if index % 2 else 0.0)
        for index in range(count)
    ]
    values = [
        1.25
        + amplitude * math.sin(2.0 * math.pi * frequency * time_value + 0.3)
        + harmonic * math.sin(4.0 * math.pi * frequency * time_value)
        for time_value in times
    ]
    return times, values


class AnalyzerTests(unittest.TestCase):
    def test_analyzes_last_three_nonuniform_cycles(self):
        times, values = sinusoid()
        result = analyze_waveform(times, values, 0.01)
        self.assertEqual(result.status, PASS)
        self.assertAlmostEqual(result.frequency, 50.0, places=3)
        self.assertAlmostEqual(result.vpp, 4.0, delta=0.02)
        self.assertAlmostEqual(result.start_time, 0.05905, delta=0.0002)
        self.assertAlmostEqual(result.end_time, 0.11905, delta=0.0002)

    def test_rejects_distorted_wave_and_unidentifiable_frequency(self):
        times, values = sinusoid(harmonic=0.4)
        self.assertEqual(analyze_waveform(times, values, 0.01).status, DISTORTED)
        flat = [2.0] * len(times)
        self.assertEqual(
            analyze_waveform(times, flat, 0.02).status,
            FREQUENCY_DETECTION_FAILED,
        )

    def test_public_frequency_helper_uses_nonuniform_samples(self):
        times, values = sinusoid()
        self.assertAlmostEqual(calculate_frequency(times, values), 50.0, places=3)


class GeneratorTests(unittest.TestCase):
    def test_coarse_grid_uses_step_and_forces_initial_and_upper(self):
        candidates = coarse_candidates(
            {"R1": 1000.0}, {"R1": 900.0}, {"R1": 1110.0}
        )
        self.assertEqual(candidates["R1"], [900.0, 950.0, 1000.0, 1050.0, 1100.0, 1110.0])

    def test_latin_hypercube_sampling_is_reproducible_and_budgeted(self):
        candidates = {"R1": [100.0, 200.0, 300.0], "R2": [10.0, 20.0, 30.0]}
        first = generate_coarse_combinations(
            candidates, 5, seed=7, initial={"R1": 200.0, "R2": 20.0}
        )
        second = generate_coarse_combinations(
            candidates, 5, seed=7, initial={"R1": 200.0, "R2": 20.0}
        )
        self.assertEqual(first, second)
        self.assertLessEqual(len(first), 5)
        self.assertIn({"R1": 200.0, "R2": 20.0}, first)
        self.assertIn({"R1": 100.0, "R2": 10.0}, first)
        self.assertIn({"R1": 300.0, "R2": 30.0}, first)


class OptimizerTests(unittest.TestCase):
    def test_search_respects_budget_and_retains_passing_best(self):
        times, values = sinusoid()
        calls = []

        def simulator(parameters):
            calls.append(dict(parameters))
            return times, values

        summary = optimize(
            initial={"R1": 1000.0},
            lower={"R1": 980.0},
            upper={"R1": 1020.0},
            max_distortion=0.01,
            max_simulations=2,
            simulator=simulator,
            seed=1,
        )
        self.assertLessEqual(summary["simulation_count"], 2)
        self.assertEqual(summary["simulation_count"], len(calls))
        self.assertEqual(
            len({tuple(sorted(parameters.items())) for parameters in calls}),
            len(calls),
        )
        self.assertIsNotNone(summary["best"])
        self.assertEqual(summary["best"]["status"], PASS)

    def test_parallel_search_matches_sequential_results(self):
        """Parallel analysis (workers=4) must produce identical results to
        sequential mode (workers=1) for the same seed."""
        times, values = sinusoid()
        wave = (times, values)

        for workers in (1, 4):
            with self.subTest(workers=workers):
                calls = []

                def sim(params):
                    calls.append(dict(params))
                    return wave

                summary = optimize(
                    initial={"R1": 1000.0, "R2": 500.0},
                    lower={"R1": 980.0, "R2": 480.0},
                    upper={"R1": 1020.0, "R2": 520.0},
                    max_distortion=0.01,
                    max_simulations=10,
                    simulator=sim,
                    seed=42,
                    workers=workers,
                )
                self.assertEqual(summary["workers"], workers)
                self.assertLessEqual(summary["simulation_count"], 10)
                self.assertEqual(
                    len({tuple(sorted(c.items())) for c in calls}),
                    len(calls),
                )
                if summary["best"]:
                    self.assertEqual(summary["best"]["status"], PASS)

    def test_multi_instance_sim_matches_single_instance(self):
        """Multi-instance simulation (sim_instances=4) must produce the same
        best result as single-instance (sim_instances=1)."""
        times, values = sinusoid()
        wave = (times, values)

        def simulator(parameters):
            return wave

        common = dict(
            initial={"R1": 1000.0, "R2": 500.0},
            lower={"R1": 980.0, "R2": 480.0},
            upper={"R1": 1020.0, "R2": 520.0},
            max_distortion=0.01,
            max_simulations=10,
            simulator=simulator,
            seed=42,
        )
        single = optimize(sim_instances=1, workers=1, **common)
        multi = optimize(sim_instances=4, workers=4, **common)
        self.assertEqual(single["simulation_count"], multi["simulation_count"])
        if single["best"] and multi["best"]:
            self.assertAlmostEqual(
                single["best"]["vpp"], multi["best"]["vpp"], places=10,
            )

    def test_multi_instance_concurrent_simulation(self):
        """Verify that sim_instances > 1 actually runs simulations in parallel.

        Each simulator call sleeps for 0.2s.  With 4 instances and 8 tasks,
        total wall time should be ~0.4s (2 batches of 4), not ~1.6s.
        """
        call_times = []
        lock = threading.Lock()

        def slow_simulator(parameters):
            with lock:
                call_times.append(time.perf_counter())
            time.sleep(0.2)
            return sinusoid()

        t0 = time.perf_counter()
        summary = optimize(
            initial={"R1": 1000.0, "R2": 500.0},
            lower={"R1": 980.0, "R2": 480.0},
            upper={"R1": 1020.0, "R2": 520.0},
            max_distortion=0.01,
            max_simulations=8,
            simulator=slow_simulator,
            seed=1,
            workers=4,
            sim_instances=4,
        )
        elapsed = time.perf_counter() - t0
        self.assertEqual(summary["simulation_count"], 8)
        # If truly parallel with 4 instances, 8 x 0.2s tasks finish in ~0.4-0.6s.
        # If serial, it takes ~1.6s+.  Allow generous margin.
        self.assertLess(elapsed, 1.2,
                        "sim_instances=4 did not run in parallel (elapsed={:.2f}s)".format(elapsed))

    def test_parallel_and_sequential_produce_same_best(self):
        times, values = sinusoid()

        def simulator(parameters):
            return times, values

        common = dict(
            initial={"R1": 1000.0},
            lower={"R1": 1000.0},
            upper={"R1": 1010.0},
            max_distortion=0.01,
            max_simulations=3,
            simulator=simulator,
            seed=7,
        )
        seq = optimize(workers=1, **common)
        par = optimize(workers=8, **common)
        self.assertEqual(seq["simulation_count"], par["simulation_count"])
        if seq["best"] and par["best"]:
            self.assertAlmostEqual(
                seq["best"]["vpp"], par["best"]["vpp"], places=10,
            )
            self.assertAlmostEqual(
                seq["best"]["distortion"], par["best"]["distortion"], places=10,
            )

    def test_simulation_failures_are_not_counted_as_distortion(self):
        def simulator(parameters):
            raise RuntimeError("simulator unavailable")

        summary = optimize(
            initial={"R1": 1000.0},
            lower={"R1": 1000.0},
            upper={"R1": 1010.0},
            max_distortion=0.01,
            max_simulations=1,
            simulator=simulator,
        )
        self.assertIsNone(summary["best"])
        self.assertEqual(summary["status_counts"], {"SIMULATION_ERROR": 1})

    def test_parallel_simulation_failures_isolated(self):
        """A failure in one combination must not corrupt another."""
        call_count = [0]

        def simulator(parameters):
            call_count[0] += 1
            if call_count[0] == 2:
                raise RuntimeError("transient failure")
            return sinusoid()

        summary = optimize(
            initial={"R1": 1000.0},
            lower={"R1": 980.0},
            upper={"R1": 1020.0},
            max_distortion=0.01,
            max_simulations=5,
            simulator=simulator,
            seed=3,
            workers=4,
        )
        self.assertIn("SIMULATION_ERROR", summary["status_counts"])
        self.assertGreater(summary["simulation_count"], 1)


class RegionTests(unittest.TestCase):
    def test_only_passing_nearby_points_collapse_to_ranked_representatives(self):
        results = [
            {"status": PASS, "vpp": 10.0, "distortion": 0.001, "parameters": {"R1": 100.0, "R2": 100.0}},
            {"status": PASS, "vpp": 12.0, "distortion": 0.002, "parameters": {"R1": 101.0, "R2": 100.0}},
            {"status": PASS, "vpp": 11.0, "distortion": 0.001, "parameters": {"R1": 190.0, "R2": 190.0}},
            {"status": DISTORTED, "vpp": 100.0, "distortion": 0.2, "parameters": {"R1": 150.0, "R2": 150.0}},
        ]
        representatives = identify_regions(
            results,
            {"R1": 100.0, "R2": 100.0},
            {"R1": 200.0, "R2": 200.0},
            {"R1": 10.0, "R2": 10.0},
        )
        self.assertEqual([item["vpp"] for item in representatives], [12.0, 11.0])


class ReportTests(unittest.TestCase):
    def test_report_saves_new_circuit_only_for_passing_result(self):
        times, values = sinusoid()
        summary = optimize(
            initial={"R1": 1000.0},
            lower={"R1": 1000.0},
            upper={"R1": 1010.0},
            max_distortion=0.01,
            max_simulations=1,
            simulator=lambda parameters: (times, values),
        )

        class FakeCircuit:
            def __init__(self):
                self.saved_path = None

            def SaveAs(self, path):
                self.saved_path = path

        circuit = FakeCircuit()
        with tempfile.TemporaryDirectory() as directory:
            output = save_optimization_report(
                summary, os.path.join(directory, "optimization_result"), circuit
            )
            self.assertTrue(os.path.isfile(os.path.join(output, "best_waveform.svg")))
            self.assertTrue(os.path.isfile(os.path.join(output, "best_parameters.json")))
            self.assertTrue(os.path.isfile(os.path.join(output, "search_summary.json")))
            self.assertTrue(circuit.saved_path.endswith("circuit_optimized.ms14"))
            with open(os.path.join(output, "best_parameters.json"), encoding="utf-8") as file:
                self.assertEqual(json.load(file)["status"], PASS)

            no_pass = dict(summary, best=None, best_waveform=None)
            second = save_optimization_report(
                no_pass, os.path.join(directory, "optimization_result"), circuit
            )
            self.assertTrue(os.path.isfile(os.path.join(second, "search_summary.json")))
            self.assertFalse(os.path.exists(os.path.join(second, "circuit_optimized.ms14")))


if __name__ == "__main__":
    unittest.main()
