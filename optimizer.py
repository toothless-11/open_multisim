"""Budgeted coarse-to-fine circuit optimization with multi-instance parallel simulation."""

import math
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from analyzer import (
    AnalysisResult,
    SIMULATION_ERROR,
    analyze_waveform,
)
from generator import (
    coarse_candidates,
    generate_coarse_combinations,
    generate_fine_candidates,
    generate_fine_combinations,
)
from region import identify_regions


def _combination_key(parameters):
    return tuple(
        (ref, round(float(value), 12))
        for ref, value in sorted(parameters.items())
    )


def _build_record(parameters, result, waveform, phase):
    """Construct a result record dict from an AnalysisResult."""
    return {
        "parameters": dict(parameters),
        "status": result.status,
        "distortion": result.distortion,
        "frequency": result.frequency,
        "vpp": result.vpp,
        "maximum": result.maximum,
        "minimum": result.minimum,
        "mean": result.mean,
        "rms": result.rms,
        "ac_rms": result.ac_rms,
        "start_time": result.start_time,
        "end_time": result.end_time,
        "num_points": result.num_points,
        "message": result.message,
        "phase": phase,
    }


def _simulate_and_analyze(simulator, parameters, threshold):
    """Run one simulation and its analysis.  Returns (record, waveform).

    This is the unit of work submitted to the thread pool.  When using
    multiple MultisimWorker instances, *simulator* dispatches to a free
    worker; when using a single instance, it calls the original closure.
    """
    # Strip internal keys before passing to simulator or storing in record
    clean = {k: v for k, v in parameters.items() if not k.startswith("_")}
    phase = parameters.get("_phase", "coarse")
    try:
        times, values = simulator(dict(clean))
        result = analyze_waveform(times, values, threshold)
        waveform = (times, values)
    except Exception as error:
        result = AnalysisResult(
            SIMULATION_ERROR,
            message="{}: {}".format(type(error).__name__, error),
        )
        waveform = None
    record = _build_record(clean, result, waveform, phase)
    return record, waveform


class _ParallelEvaluator:
    """Distributes simulations across multiple workers and analyses in parallel.

    When *sim_instances* > 1, simulations run on separate MultisimWorker
    threads.  Analysis runs on a separate ThreadPoolExecutor, overlapping
    with both simulation and analysis of other combinations.

    When *sim_instances* == 1, falls back to the original behaviour:
    the single simulator closure is called, and analysis is dispatched
    to the analysis thread pool (pipeline overlap).
    """

    def __init__(self, simulator, threshold, workers=1, sim_instances=1):
        self._simulator = simulator
        self._threshold = threshold
        self._workers = max(1, int(workers))
        self._sim_instances = max(1, int(sim_instances))
        # Thread pool for both simulation (when multi-instance) and analysis.
        # When sim_instances > 1, the pool runs simulate+analyze as a unit.
        # When sim_instances == 1, the pool only runs analysis (simulator
        # is called on the main thread).
        self._executor = ThreadPoolExecutor(max_workers=self._workers)
        self._lock = threading.Lock()
        self._pending = []
        self.records = []
        self.cache = {}
        self.counts = Counter()
        self.simulations = 0
        self.best_waveform_record = None
        self.best_waveform = None
        self._closed = False

    def evaluate(self, parameters, phase):
        """Submit one combination for evaluation.

        With multiple simulation instances, both simulation and analysis
        happen in the thread pool (truly parallel).

        With a single instance, simulation runs on the main thread and
        only analysis is dispatched to the pool (pipeline overlap).
        """
        key = _combination_key(parameters)
        with self._lock:
            if key in self.cache:
                return self.cache[key]

        if self._sim_instances > 1:
            # Multi-instance: submit simulation + analysis as one task
            tagged = dict(parameters)
            tagged["_phase"] = phase
            future = self._executor.submit(
                _simulate_and_analyze, self._simulator, tagged, self._threshold
            )
            with self._lock:
                self._pending.append((parameters, phase, future))
                self.simulations += 1
            return None
        else:
            # Single instance: simulate on main thread, analyze in pool
            try:
                times, values = self._simulator(dict(parameters))
                waveform = (times, values)
            except Exception as error:
                result = AnalysisResult(
                    SIMULATION_ERROR,
                    message="{}: {}".format(type(error).__name__, error),
                )
                record = _build_record(parameters, result, None, phase)
                with self._lock:
                    self.records.append(record)
                    self.cache[key] = record
                    self.counts[result.status] += 1
                    self.simulations += 1
                    self._update_best(record, None)
                return record

            future = self._executor.submit(
                analyze_waveform, waveform[0], waveform[1], self._threshold
            )
            with self._lock:
                self._pending.append((parameters, phase, waveform, future))
                self.simulations += 1
            return None

    def drain(self):
        """Wait for all pending tasks and collect their records."""
        with self._lock:
            pending = list(self._pending)
            self._pending.clear()

        for item in pending:
            # Multi-instance format: (parameters, phase, future_of_record)
            # Single-instance format: (parameters, phase, waveform, future_of_analysis)
            if len(item) == 3:
                parameters, phase, future = item
                try:
                    record, waveform = future.result()
                    # Override phase from the tagged dict
                    record["phase"] = phase
                except Exception as error:
                    result = AnalysisResult(
                        SIMULATION_ERROR,
                        message="worker thread: {}: {}".format(
                            type(error).__name__, error
                        ),
                    )
                    record = _build_record(parameters, result, None, phase)
                    waveform = None
            else:
                parameters, phase, waveform, future = item
                try:
                    result = future.result()
                except Exception as error:
                    result = AnalysisResult(
                        SIMULATION_ERROR,
                        message="analysis thread: {}: {}".format(
                            type(error).__name__, error
                        ),
                    )
                record = _build_record(parameters, result, waveform, phase)

            with self._lock:
                self.records.append(record)
                key = _combination_key(parameters)
                self.cache[key] = record
                self.counts[record["status"]] += 1
                self._update_best(record, waveform)

    def in_cache(self, parameters):
        return _combination_key(parameters) in self.cache

    def cached_record(self, parameters):
        return self.cache.get(_combination_key(parameters))

    def close(self):
        if not self._closed:
            self._executor.shutdown(wait=True)
            self._closed = True

    def _update_best(self, record, waveform):
        """Update best-waveform tracking.  Caller must hold self._lock."""
        if record["status"] == "PASS" and record["vpp"] is not None:
            if (
                self.best_waveform_record is None
                or (record["vpp"], -record["distortion"])
                > (
                    self.best_waveform_record["vpp"],
                    -self.best_waveform_record["distortion"],
                )
            ):
                self.best_waveform_record = record
                self.best_waveform = waveform


# ------------------------------------------------------------------
# Worker-pool simulator factory
# ------------------------------------------------------------------

def create_worker_pool_simulator(workers, circuit_path, output_name, sim_params):
    """Create a simulator function that dispatches to multiple MultisimWorkers.

    Parameters
    ----------
    workers : list of MultisimWorker
        Already-started worker instances.
    circuit_path : str
        Not used directly (workers already have it).
    output_name : str
        Not used directly (workers already have it).
    sim_params : dict
        Not used directly (workers already have simulation params).

    Returns
    -------
    callable
        ``simulator(parameters) -> (times, values)`` that picks the next
        available worker and runs the simulation on it.
    """
    import queue

    worker_queue = queue.Queue()
    for w in workers:
        worker_queue.put(w)

    def simulator(parameters):
        # Clean up internal keys
        params = {k: v for k, v in parameters.items() if not k.startswith("_")}
        worker = worker_queue.get(timeout=60)
        try:
            return worker.simulate(params)
        finally:
            worker_queue.put(worker)

    return simulator


# ------------------------------------------------------------------
# Optimizer entry point
# ------------------------------------------------------------------

def optimize(
    initial,
    lower,
    upper,
    max_distortion,
    max_simulations,
    simulator,
    seed=None,
    workers=1,
    sim_instances=1,
):
    """Run a bounded search with parallel simulation and analysis.

    Parameters
    ----------
    initial : dict
        ``{ref: resistance}`` – initial resistor values.
    lower, upper : dict
        Per-resistor search bounds.
    max_distortion : float
        Normalised sine-fit residual threshold (e.g. 0.02 for 2 %).
    max_simulations : int
        Total simulation budget.
    simulator : callable
        ``simulator(parameters) -> (times, values)``.
        When *sim_instances* > 1 this should be a worker-pool dispatcher
        created by ``create_worker_pool_simulator``.
        When *sim_instances* == 1 it is the original single-circuit closure.
    seed : int or None
        RNG seed for reproducible Latin-hypercube sampling.
    workers : int
        Number of analysis threads.
    sim_instances : int
        Number of parallel simulation instances.  ``1`` uses the original
        single-circuit serial simulation path.

    Returns
    -------
    dict
        Search summary.
    """
    # ---- input validation ----
    if not isinstance(max_simulations, int) or isinstance(max_simulations, bool):
        raise ValueError("max_simulations must be a positive integer")
    if max_simulations <= 0:
        raise ValueError("max_simulations must be a positive integer")
    if not initial or set(initial) != set(lower) or set(initial) != set(upper):
        raise ValueError("initial, lower, and upper must contain the same resistors")
    if len(set(initial)) != len(initial):
        raise ValueError("duplicate resistor references")
    for ref, value in initial.items():
        low = float(lower[ref])
        high = float(upper[ref])
        if not math.isfinite(float(value)) or float(value) <= 0.0:
            raise ValueError("initial resistance for {} must be positive".format(ref))
        if not math.isfinite(low) or not math.isfinite(high):
            raise ValueError("search bounds for {} must be finite".format(ref))
        if not (0.0 < low < high):
            raise ValueError("invalid search bounds for {}".format(ref))
        if not low <= float(value) <= high:
            raise ValueError("initial resistance for {} is outside search bounds".format(ref))
    threshold = float(max_distortion)
    if not math.isfinite(threshold) or threshold <= 0.0:
        raise ValueError("max_distortion must be a finite positive number")
    if not isinstance(workers, int) or isinstance(workers, bool) or workers < 1:
        raise ValueError("workers must be a positive integer")
    if not isinstance(sim_instances, int) or isinstance(sim_instances, bool) or sim_instances < 1:
        raise ValueError("sim_instances must be a positive integer")

    # When multiple sim instances, the thread pool runs both sim + analysis,
    # so we need enough threads.  workers should be >= sim_instances.
    effective_workers = max(workers, sim_instances)

    # ---- coarse search setup ----
    candidates = coarse_candidates(initial, lower, upper)
    theoretical = math.prod(len(values) for values in candidates.values())
    coarse_budget = min(
        max_simulations,
        max(1, math.floor(0.7 * max_simulations)),
    )
    coarse_combinations = generate_coarse_combinations(
        candidates, coarse_budget, seed=seed, initial=initial
    )

    evaluator = _ParallelEvaluator(
        simulator, threshold,
        workers=effective_workers,
        sim_instances=sim_instances,
    )
    try:
        # ---- coarse search ----
        for parameters in coarse_combinations:
            evaluator.evaluate(parameters, "coarse")

        evaluator.drain()

        coarse_steps = {
            ref: max(15.0, 0.05 * float(value))
            for ref, value in initial.items()
        }
        regions = identify_regions(
            evaluator.records, lower, upper, coarse_steps
        )

        # ---- fine search ----
        fine_completed = True
        fine_budget = max_simulations - evaluator.simulations
        fine_used = 0
        for region in regions:
            fine_candidates = generate_fine_candidates(
                region["parameters"], lower, upper, coarse_steps
            )
            for parameters in generate_fine_combinations(fine_candidates):
                if evaluator.in_cache(parameters):
                    continue
                if evaluator.simulations >= max_simulations:
                    fine_completed = False
                    break
                if fine_used >= fine_budget:
                    fine_completed = False
                    break
                evaluator.evaluate(parameters, "fine")
                fine_used += 1
            evaluator.drain()
            if evaluator.simulations >= max_simulations and not fine_completed:
                break
    finally:
        evaluator.drain()
        evaluator.close()

    # ---- final selection ----
    passing = [
        record for record in evaluator.records
        if record["status"] == "PASS" and record["vpp"] is not None
    ]
    best = None
    if passing:
        best = max(
            passing,
            key=lambda record: (float(record["vpp"]), -float(record["distortion"])),
        )

    simulations = evaluator.simulations
    budget_exhausted = simulations >= max_simulations and (
        len(coarse_combinations) < theoretical or not fine_completed
    )
    best_waveform_record = evaluator.best_waveform_record
    best_waveform = evaluator.best_waveform
    summary = {
        "status": "PASS" if best else "NO_QUALIFIED_COMBINATION",
        "best": best,
        "results": evaluator.records,
        "simulation_count": simulations,
        "max_simulations": max_simulations,
        "coarse_budget": coarse_budget,
        "coarse_sample_count": len(coarse_combinations),
        "theoretical_coarse_combinations": theoretical,
        "fine_completed": fine_completed,
        "budget_exhausted": budget_exhausted,
        "search_complete": not budget_exhausted,
        "status_counts": dict(evaluator.counts),
        "regions": regions,
        "distortion_threshold": threshold,
        "seed": seed,
        "workers": workers,
        "sim_instances": sim_instances,
        "best_waveform": (
            {"times": best_waveform[0], "values": best_waveform[1]}
            if best_waveform_record is best and best_waveform is not None
            else None
        ),
    }
    return summary
