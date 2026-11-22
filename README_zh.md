# Open Multisim

**让电路仿真自动化，让设计回归电路本身。**

Open Multisim 是一个基于 Python 和 Multisim COM API 的电路仿真自动化工具，目前包含两个独立功能：

- **自动电路参数优化** —— 通过反复仿真和参数搜索，自动寻找满足目标要求的电路参数。
- **Multisim 自动启动** —— 自动启动 Multisim，检测并处理启动异常，获得可正常使用的 Multisim 元件库窗口。

## 语言

- [English](README.md)
- [简体中文](README_zh.md)

## 功能介绍

### 1. 自动电路参数优化

通过 Python 调用 Multisim COM API，将电路仿真与参数搜索结合，实现电路参数的自动优化。

主要功能：

- 自动读取电路中的电阻及其初始阻值。
- 自定义需要调整的元件和搜索范围。
- 指定输出探针和优化目标。
- 自动执行瞬态仿真。
- 通过正弦拟合误差评估输出波形。
- 支持多个 Multisim 实例并行仿真。
- 自动生成优化报告并保存优化后的电路文件。

**运行方式：**

```bash
python main.py
```

### 2. Multisim 自动启动

自动启动 Multisim，并处理启动过程中可能出现的异常。

主要功能：

- 自动检测 Multisim 安装路径。
- 批量启动多个 Multisim 实例进行启动检测。
- 检测启动异常和错误窗口。
- 自动清理启动失败的进程及其子进程。
- 保留成功启动的窗口，关闭多余实例。
- 启动失败时自动清理并重试。

**运行方式：**

```bash
python open_multisim.py
```

## 环境要求

- Windows 10 / 11
- 已安装 NI Multisim
- Python 3
- 支持 COM 自动化的 Multisim 环境

安装依赖：

```bash
pip install pywin32 wmi
```

自动调参模块可能还需要其他数值计算及优化相关依赖。

## 项目结构

```text
open_multisim/
├── main.py              # 自动调参程序入口
├── optimizer.py         # 参数优化算法
├── analyzer.py          # 仿真波形分析
├── generator.py         # 参数及候选方案生成
├── region.py            # 搜索区域管理
├── report.py            # 优化结果与报告生成
├── multisim_api.py      # Multisim COM API 封装
├── open_multisim.py     # Multisim 自动启动工具
└── tests/               # 测试文件
```

## 注意事项

- 自动调参功能需要有效的 `.ms14` 电路文件。
- 电路中需要存在可读取的电阻和输出探针。
- 优化过程中不会直接覆盖原始电路文件。
- 并行仿真会占用额外的 CPU 和内存资源。
- 自动启动功能涉及多个 Multisim 进程的创建和清理，请注意其他正在运行的 Multisim 实例。

## 项目目的

将重复性的 Multisim 操作交给 Python 自动完成，让电路设计者能够把更多精力放在电路分析、参数选择和设计验证上。

**让仿真自动化，让设计回归电路本身。**
