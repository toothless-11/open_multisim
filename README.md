# Open Multisim

**Automate Multisim. Focus on circuit design.**

Open Multisim is a Python-based automation toolkit for NI Multisim, built on the Windows COM API.

It currently provides two independent tools:

- **Automatic Circuit Optimization** — Automatically adjusts circuit parameters through iterative simulations and parameter search.
- **Multisim Launcher** — Automatically launches Multisim and handles startup issues to obtain a working Multisim window with the component library.

## Language

- [English](README.md)
- [简体中文](README_zh.md)

## Features

### 1. Automatic Circuit Optimization

Automates circuit parameter optimization by combining Multisim simulations with parameter search algorithms.

Key features:

- Extract resistor parameters and initial values from circuit files.
- Configure adjustable components and search ranges.
- Select output probes and define optimization targets.
- Run transient simulations automatically.
- Evaluate output waveforms using sinusoidal fitting error.
- Support parallel simulations using multiple Multisim instances.
- Generate optimization reports and save optimized circuit files.

**Run:**

```bash
python main.py
```

### 2. Multisim Launcher

Automatically launches Multisim and manages potential startup failures.

Key features:

- Detect the Multisim installation path.
- Launch multiple Multisim instances for startup verification.
- Detect startup errors and abnormal windows.
- Clean up failed processes and their child processes.
- Keep a successfully launched window and close redundant instances.
- Retry automatically when startup fails.

**Run:**

```bash
python open_multisim.py
```

## Requirements

- Windows 10 / 11
- NI Multisim
- Python 3
- Multisim COM automation support

Install dependencies:

```bash
pip install pywin32 wmi
```

Additional dependencies may be required by the optimization module.

## Project Structure

```text
open_multisim/
├── main.py
├── optimizer.py
├── analyzer.py
├── generator.py
├── region.py
├── report.py
├── multisim_api.py
├── open_multisim.py
└── tests/
```

## Notes

- The optimization tool requires a valid `.ms14` circuit file.
- The circuit should contain accessible resistors and output probes.
- The original circuit file is not directly overwritten during optimization.
- Parallel simulations consume additional CPU and memory resources.
- The launcher manages multiple Multisim processes. Use caution if other Multisim instances are running.

## Project Goal

Reduce repetitive operations in circuit simulation and allow designers to focus on circuit analysis, parameter selection, and design verification.

**Automate the simulation. Focus on the circuit.**
