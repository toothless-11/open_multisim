"""Interactive entry point for Multisim circuit optimization."""

import math
import os

from multisim_api import (
    MultisimWorker,
    close_multisim,
    connect_multisim,
    find_resistors,
    get_all_outputs,
    get_waveform,
    set_rlc_values,
)
from optimizer import optimize, create_worker_pool_simulator
from report import save_optimization_report


def _read_number(prompt, minimum=None, default=None):
    while True:
        try:
            text = input(prompt).strip()
            value = float(default) if not text and default is not None else float(text)
            if not math.isfinite(value) or (minimum is not None and value < minimum):
                raise ValueError
            return value
        except ValueError:
            print("请输入有效数字。")


def _read_positive_integer(prompt, default=None):
    while True:
        try:
            text = input(prompt).strip()
            value = int(default) if not text and default is not None else int(text)
            if value <= 0:
                raise ValueError
            return value
        except ValueError:
            print("请输入正整数。")


def _collect_parameters(resistors):
    references = input("可调电阻参考标号（逗号分隔）: ").strip().upper().split(",")
    references = [reference.strip() for reference in references if reference.strip()]
    if not references:
        raise ValueError("至少需要指定一个可调电阻")
    if len(set(references)) != len(references):
        raise ValueError("电阻参考标号不能重复")

    lookup = {str(ref).upper(): ref for ref in resistors}
    unknown = [ref for ref in references if ref not in lookup]
    if unknown:
        raise ValueError(
            "电路中不存在电阻 {}；可用电阻：{}".format(
                ", ".join(unknown), ", ".join(sorted(lookup))
            )
        )
    initial = {lookup[ref]: resistors[lookup[ref]] for ref in references}
    lower = {}
    upper = {}
    for ref, initial_value in initial.items():
        while True:
            print("{} 初始阻值：{} Ohm".format(ref, initial_value))
            low = _read_number("{} 搜索下限 (Ohm): ".format(ref), minimum=0.0)
            high = _read_number("{} 搜索上限 (Ohm): ".format(ref), minimum=0.0)
            if low >= high:
                print("搜索下限必须小于上限。")
                continue
            if low <= initial_value <= high:
                lower[ref], upper[ref] = low, high
                break
            print("警告：初始值不在指定范围内，不能直接开始优化。")
            answer = input(
                "是否明确将范围扩展以包含初始值（y/n）？"
            ).strip().lower()
            if answer == "y":
                lower[ref] = min(low, initial_value)
                upper[ref] = max(high, initial_value)
                break
    return initial, lower, upper


def main():
    circuit_path = input("Multisim 电路文件路径 (.ms14): ").strip().strip('"')
    if not circuit_path.lower().endswith(".ms14") or not os.path.isfile(circuit_path):
        raise ValueError("请提供存在的 .ms14 文件")
    print(
        "注意：搜索会修改 Multisim 当前内存中的电路对象，但不会保存覆盖原文件。"
        "若电路在 GUI 中有未保存的修改，请先保存原始电路。"
    )
    if input("确认继续？(y/n): ").strip().lower() != "y":
        print("已取消。")
        return

    abspath = os.path.abspath(circuit_path)

    # ---- read circuit metadata from a single connection ----
    app = connect_multisim()
    try:
        circuit = app.OpenFile(abspath)
        resistors = find_resistors(circuit)
        if not resistors:
            raise RuntimeError("电路中未检测到可读取的电阻")
        initial, lower, upper = _collect_parameters(resistors)

        outputs = list(get_all_outputs(circuit))
        if not outputs:
            raise RuntimeError("电路中没有可用输出探针")
        print("可用输出探针：{}".format(", ".join(map(str, outputs))))
        output_name = input("待优化输出探针名称: ").strip()
        if output_name not in outputs:
            raise ValueError("无效输出探针：{}".format(output_name))

        distortion_percent = _read_number("最大正弦拟合误差（百分比，例如 2）: ", minimum=0.0)
        if distortion_percent <= 0.0:
            raise ValueError("失真阈值必须大于零")
        max_simulations = _read_positive_integer("最大仿真次数: ")
        seed_text = input("随机种子（留空表示不指定）: ").strip()
        seed = int(seed_text) if seed_text else None
        duration = _read_number("瞬态仿真时长（秒，默认 0.01）: ", minimum=0.0, default=0.01)
        if duration <= 0.0:
            raise ValueError("仿真时长必须大于零")
        sample_rate = _read_number("采样参数（默认 10000）: ", minimum=0.0, default=10000)
        if sample_rate <= 0.0:
            raise ValueError("采样参数必须大于零")
        num_samples = _read_positive_integer("请求样本数（默认 1000）: ", default=1000)

        cpu_count = os.cpu_count() or 1
        sim_instances = _read_positive_integer(
            "并行仿真实例数（默认 1，增大可开多个 Multisim 连接）: ",
            default=1,
        )
        workers = _read_positive_integer(
            "并行分析线程数（默认 {}）: ".format(cpu_count),
            default=cpu_count,
        )

        # Release the metadata connection; workers will open their own
        circuit = None
    finally:
        close_multisim()

    # ---- build simulator ----
    if sim_instances > 1:
        print("正在启动 {} 个 Multisim 仿真实例...".format(sim_instances))
        workers_list = []
        sim_params = {
            "duration": duration,
            "sample_rate": sample_rate,
            "num_samples": num_samples,
        }
        for i in range(sim_instances):
            w = MultisimWorker(
                abspath,
                output_name,
                duration=duration,
                sample_rate=sample_rate,
                num_samples=num_samples,
                label=str(i),
            )
            w.start()
            workers_list.append(w)
            print("  实例 {}/{} 已就绪".format(i + 1, sim_instances))
        simulator = create_worker_pool_simulator(
            workers_list, abspath, output_name, sim_params
        )
    else:
        workers_list = []

        def simulator(parameters):
            app2 = connect_multisim()
            try:
                c = app2.OpenFile(abspath)
                set_rlc_values(c, parameters)
                return get_waveform(
                    c,
                    output_name,
                    duration=duration,
                    sample_rate=sample_rate,
                    num_samples=num_samples,
                )
            finally:
                close_multisim()

    try:
        summary = optimize(
            initial=initial,
            lower=lower,
            upper=upper,
            max_distortion=distortion_percent / 100.0,
            max_simulations=max_simulations,
            simulator=simulator,
            seed=seed,
            workers=workers,
            sim_instances=sim_instances,
        )

        # Set best parameters on one worker/circuit for SaveAs
        if summary["best"] is not None:
            if workers_list:
                workers_list[0].set_resistors(summary["best"]["parameters"])
                save_circuit = workers_list[0].circuit
            else:
                # Single-instance mode: open a fresh circuit for saving
                app2 = connect_multisim()
                c = app2.OpenFile(abspath)
                set_rlc_values(c, summary["best"]["parameters"])
                save_circuit = c

        result_directory = save_optimization_report(
            summary,
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "optimization_result"),
            circuit=save_circuit if summary["best"] is not None else None,
        )
        print("\n搜索结束：{}".format(summary["status"]))
        print("仿真次数：{}/{}".format(
            summary["simulation_count"], summary["max_simulations"]
        ))
        print("预算耗尽：{}".format("是" if summary["budget_exhausted"] else "否"))
        print("仿真实例：{}".format(sim_instances))
        if summary["best"] is not None:
            best = summary["best"]
            print("最佳参数：{}".format(best["parameters"]))
            print("实际 Vpp：{:.9g} V".format(best["vpp"]))
            print("拟合误差：{:.4%}".format(best["distortion"]))
            print("结果电路已另存为新文件。")
        else:
            print("没有满足失真阈值的组合；未生成优化电路。")
        print("结果目录：{}".format(result_directory))
    finally:
        for w in workers_list:
            w.shutdown()
        if not workers_list:
            close_multisim()


if __name__ == "__main__":
    main()
