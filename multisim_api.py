"""Multisim COM API with multi-instance parallel simulation support.

Each ``MultisimWorker`` wraps an independent COM connection that runs
simulations in its own thread with its own COM apartment.  This allows
multiple simulations to execute concurrently when the Multisim COM
server supports it (In-Process, Apartment-threaded).
"""

import math
import time
import html
import threading


# ============================================================
# 1. 连接管理
# ============================================================

def connect_multisim():
    """
    初始化 COM 并连接 Multisim。

    返回：
        app: Multisim 应用对象

    注意：必须在调用线程中执行，COM apartment 绑定到当前线程。
    """
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()

    try:
        app = win32com.client.gencache.EnsureDispatch(
            "MultisimInterface.MultisimApp"
        )

        app.Connect()

        return app

    except Exception:
        pythoncom.CoUninitialize()
        raise


def open_circuit(app, circuit_path):
    """
    打开指定电路。

    返回：
        circuit: Multisim 电路对象
    """
    return app.OpenFile(circuit_path)


def close_multisim():
    """
    释放当前线程的 COM 初始化。

    注意：不主动关闭 Multisim 软件。
    """
    import pythoncom

    pythoncom.CoUninitialize()


# ============================================================
# 2. 电路查询
# ============================================================

def get_all_components(circuit):
    """
    获取电路中的全部元件参考标号。
    """
    components = circuit.EnumComponents(0)

    return tuple(components or ())


def get_all_outputs(circuit):
    """
    获取电路中的全部输出探针。
    """
    outputs = circuit.EnumOutputs(0)

    return tuple(outputs or ())


def get_rlc_value(circuit, component_ref):
    """
    读取一个 RLC 元件的参数。
    """
    return float(circuit.RLCValue(component_ref))


def find_resistors(circuit):
    """
    自动识别电阻并读取阻值。

    当前识别规则：
    1. 参考标号以 R 开头。
    2. RLCValue 能够成功读取。

    """
    components = get_all_components(circuit)

    resistors = {}
    failures = []

    for ref in components:

        if not str(ref).upper().startswith("R"):
            continue

        try:
            value = get_rlc_value(circuit, ref)
            if not math.isfinite(value) or value <= 0.0:
                failures.append("{} (invalid value {})".format(ref, value))
            else:
                resistors[str(ref)] = value

        except Exception as error:
            failures.append("{} ({})".format(ref, error))

    if failures:
        raise RuntimeError(
            "Unable to read resistor values: {}".format("; ".join(failures))
        )
    return resistors


# ============================================================
# 3. 电路修改
# ============================================================

def set_rlc_value(circuit, component_ref, value):
    """
    修改一个 RLC 元件的参数。

    返回实际读取到的参数值。
    """
    circuit.SetRLCValue(
        component_ref,
        float(value)
    )

    return get_rlc_value(circuit, component_ref)


def set_rlc_values(circuit, values):
    """
    批量修改 RLC 元件。

    参数示例：
        {
            "R1": 1000,
            "R2": 2200
        }

    返回实际设置值。
    """
    actual_values = {}

    for ref, value in values.items():

        actual_values[ref] = set_rlc_value(
            circuit,
            ref,
            value
        )

    return actual_values


# ============================================================
# 4. 输出探针与数据请求
# ============================================================

def make_output_array(outputs):
    """
    将 Python 输出列表转换为 COM SAFEARRAY。
    """
    import pythoncom
    import win32com.client

    return win32com.client.VARIANT(
        pythoncom.VT_ARRAY | pythoncom.VT_BSTR,
        tuple(outputs)
    )


def register_outputs(circuit, outputs):
    """
    为输出探针登记数据请求。

    当前已验证参数：
        method=0
        sampleRate=1.0
        numSamples=1
        repeatFlag=False
    """
    for name in outputs:

        circuit.SetOutputRequest(
            name,
            0,
            1.0,
            1,
            False
        )


# ============================================================
# 5. 仿真控制
# ============================================================

def run_dc(circuit, outputs):
    """
    登记输出请求并执行 DC 工作点分析。
    """
    if not outputs:
        raise ValueError("没有可用的输出探针")

    register_outputs(circuit, outputs)

    output_array = make_output_array(outputs)

    circuit.DoDCOperatingPoint(output_array)


def wait_for_outputs(circuit, outputs, timeout=15):
    """
    等待所有输出探针的数据就绪。

    超时则抛出 TimeoutError。
    """
    pending = set(outputs)

    start = time.time()

    while pending and time.time() - start < timeout:

        for name in list(pending):

            if circuit.OutputReady(name):
                pending.remove(name)

        if pending:
            time.sleep(0.1)

    if pending:
        raise TimeoutError(
            f"输出等待超时：{pending}"
        )


# ============================================================
# 6. 数据读取
# ============================================================

def read_output(circuit, output_name):
    """
    读取一个输出探针的 DC 数值。

    当前适用于已验证的数据结构：
        (((时间,), (数值,)), 0)
    """
    data = circuit.GetOutputData(output_name)

    return float(data[0][1][0])


def read_outputs(circuit, outputs):
    """
    读取所有输出探针。

    返回：
        {
            "V(PR1)": 5.0,
            "I(PR2)": 0.005
        }
    """
    results = {}

    for name in outputs:
        results[name] = read_output(circuit, name)

    return results


# ============================================================
# 7. 文件操作
# ============================================================

def save_circuit(circuit):
    """
    保存当前电路。
    """
    circuit.Save()


def save_circuit_as(circuit, file_path):
    """
    将当前电路另存为。
    """
    circuit.SaveAs(file_path)


def calculate_vpp(values):
    """计算峰峰值"""
    values = [float(x) for x in values]

    if not values:
        raise ValueError("波形数据为空")

    return max(values) - min(values)

# ============================================================
# 8. 波形获取
# ============================================================

def wait_output_ready(circuit, output_name, timeout=30, interval=0.05):
    """
    等待指定输出探针的数据准备完成。

    参数：
        circuit: Multisim 电路对象
        output_name: 输出名称，例如 "V(PR1)"
        timeout: 最大等待时间，单位秒
        interval: 轮询间隔，单位秒

    返回：
        True: 数据准备完成

    异常：
        TimeoutError: 超时仍未准备完成
    """

    start_time = time.time()

    while True:
        if circuit.OutputReady(output_name):
            return True

        if time.time() - start_time > timeout:
            raise TimeoutError(
                f"等待输出数据超时：{output_name}"
            )

        time.sleep(interval)


def get_waveform(
    circuit,
    output_name,
    duration=0.1,
    sample_rate=10000.0,
    num_samples=1000,
    timeout=30
):
    """
    执行瞬态仿真并读取指定探针的波形。

    参数：
        circuit: Multisim 电路对象
        output_name: 输出名称，例如 "V(PR1)"
        duration: 仿真时长，单位秒
        sample_rate: 输出请求参数
        num_samples: 输出请求样本数量
        timeout: 等待数据准备的最大时间

    返回：
        times: 时间数组
        values: 波形数值数组
    """

    # 设置输出数据请求
    circuit.SetOutputRequest(
        output_name,
        0,
        float(sample_rate),
        int(num_samples),
        False
    )

    # 执行瞬态仿真
    circuit.RunSimulation(
        float(duration),
        False
    )

    # 等待输出数据准备完成
    wait_output_ready(
        circuit,
        output_name,
        timeout=timeout
    )

    # 获取原始数据
    data = circuit.GetOutputData(output_name)

    # 提取时间数组和波形数组
    times = data[0][0]
    values = data[0][1]

    if len(times) != len(values):
        raise ValueError("时间数组与波形数组长度不一致")

    return times, values


def calculate_waveform(times, values):
    """
    分析波形数据。

    计算：
        最大值
        最小值
        峰峰值
        平均值
        RMS（包含直流分量）
        AC RMS（去除直流分量）

    使用梯形积分，支持非均匀时间间隔。

    返回：
        dict: 波形分析结果
    """

    if len(times) != len(values):
        raise ValueError("时间数组与波形数组长度不一致")

    if len(times) < 2:
        raise ValueError("至少需要两个采样点")

    times = [float(x) for x in times]
    values = [float(y) for y in values]
    if not all(math.isfinite(x) for x in times + values):
        raise ValueError("波形数据包含无效数值")

    # 检查时间是否严格递增
    for i in range(1, len(times)):
        if times[i] <= times[i - 1]:
            raise ValueError("时间数据必须严格递增")

    v_max = max(values)
    v_min = min(values)
    v_pp = v_max - v_min

    duration = times[-1] - times[0]

    if duration <= 0:
        raise ValueError("波形时间范围无效")

    integral = 0.0
    square_integral = 0.0

    # 梯形积分
    for i in range(len(times) - 1):

        dt = times[i + 1] - times[i]

        y1 = values[i]
        y2 = values[i + 1]

        integral += (y1 + y2) * dt / 2

        square_integral += (
            (y1 ** 2 + y2 ** 2) * dt / 2
        )

    # 平均值
    mean = integral / duration

    # 均方值
    mean_square = square_integral / duration

    # RMS，包含直流分量
    rms = math.sqrt(max(0.0, mean_square))

    # AC RMS，去除直流分量
    ac_rms = math.sqrt(
        max(0.0, mean_square - mean ** 2)
    )

    return {
        "max": v_max,
        "min": v_min,
        "vpp": v_pp,
        "mean": mean,
        "rms": rms,
        "ac_rms": ac_rms,
        "duration": duration,
        "num_points": len(times)
    }

def calculate_frequency(times, values):
    """
    根据最后四个上升平均值穿越点估算三个周期的频率。

    返回：
        frequency: 估算频率，单位 Hz
    """
    from analyzer import estimate_frequency

    return estimate_frequency(times, values)



def save_waveform_image(
    times,
    values,
    path="waveform.svg",
    title="Waveform",
    x_label="Time (s)",
    y_label="Voltage (V)",
    cycles=3,
    frequency=None,
    time_range=None
):
    """
    绘制指定周期数的波形，默认显示三个完整周期。

    frequency:
        信号频率，单位 Hz。
        如果不指定，则根据波形数据估算频率。
    """

    if len(times) != len(values):
        raise ValueError("时间数组与波形数组长度不一致")

    if len(times) < 2:
        raise ValueError("至少需要两个采样点")

    if cycles <= 0:
        raise ValueError("cycles 必须大于 0")

    times = [float(t) for t in times]
    values = [float(v) for v in values]

    if not all(math.isfinite(x) for x in times + values):
        raise ValueError("波形数据包含无效数值")

    if any(times[i] <= times[i - 1]
           for i in range(1, len(times))):
        raise ValueError("时间数据必须严格递增")

    # 自动估算频率：统计上升穿越平均值的位置
    if frequency is None:
        frequency = calculate_frequency(times, values)

    frequency = float(frequency)

    if not math.isfinite(frequency) or frequency <= 0:
        raise ValueError("frequency 必须是有效正数")

    # Use an explicitly selected interval for optimization reports.
    if time_range is None:
        start_time = times[0]
        end_time = start_time + cycles / frequency
    else:
        start_time, end_time = map(float, time_range)
        if not (times[0] <= start_time < end_time <= times[-1]):
            raise ValueError("time_range must be within the waveform data")

    if end_time > times[-1]:
        raise ValueError(
            f"数据不足以绘制 {cycles} 个完整周期，"
            f"需要 {cycles / frequency:.6f} s，"
            f"实际只有 {times[-1] - times[0]:.6f} s"
        )

    # 截取绘图范围并插入精确的区间端点。
    plot_times = []
    plot_values = []

    for i in range(1, len(times)):
        if times[i - 1] <= start_time <= times[i]:
            ratio = (
                (start_time - times[i - 1])
                / (times[i] - times[i - 1])
            )
            start_value = values[i - 1] + ratio * (values[i] - values[i - 1])
            plot_times.append(start_time)
            plot_values.append(start_value)
            break

    for t, v in zip(times, values):
        if start_time < t < end_time:
            plot_times.append(t)
            plot_values.append(v)

    # 线性插值，确保恰好结束在第三个周期末尾
    for i in range(1, len(times)):
        if times[i - 1] <= end_time <= times[i]:
            ratio = (
                (end_time - times[i - 1])
                / (times[i] - times[i - 1])
            )

            end_value = (
                values[i - 1]
                + ratio * (values[i] - values[i - 1])
            )

            plot_times.append(end_time)
            plot_values.append(end_value)
            break

    # 图像尺寸
    width = 1200
    height = 650

    left, right = 100, 1150
    top, bottom = 80, 550

    plot_width = right - left
    plot_height = bottom - top

    v_min = min(plot_values)
    v_max = max(plot_values)

    if v_max == v_min:
        margin = max(abs(v_max) * 0.05, 1.0)
    else:
        margin = (v_max - v_min) * 0.08

    v_min -= margin
    v_max += margin

    def map_x(t):
        return left + (
            (t - start_time) / (end_time - start_time)
        ) * plot_width

    def map_y(v):
        return bottom - (
            (v - v_min) / (v_max - v_min)
        ) * plot_height

    elements = []

    elements.append(
        f'<rect width="{width}" height="{height}" fill="white"/>'
    )

    elements.append(
        f'<text x="{width / 2}" y="38" '
        f'text-anchor="middle" font-size="24">'
        f'{html.escape(title)}</text>'
    )

    # 横轴网格：按周期划分
    for i in range(2 * cycles + 1):
        x = left + plot_width * i / (2 * cycles)
        t = start_time + (
            end_time - start_time
        ) * i / (2 * cycles)

        elements.append(
            f'<line x1="{x}" y1="{top}" '
            f'x2="{x}" y2="{bottom}" '
            f'stroke="#cccccc" stroke-dasharray="5,5"/>'
        )

        elements.append(
            f'<text x="{x}" y="{bottom + 25}" '
            f'text-anchor="middle" font-size="13">'
            f'{t:.6g}</text>'
        )

    # 纵轴网格
    for i in range(6):
        y = top + plot_height * i / 5
        v = v_max - (v_max - v_min) * i / 5

        elements.append(
            f'<line x1="{left}" y1="{y}" '
            f'x2="{right}" y2="{y}" '
            f'stroke="#cccccc" stroke-dasharray="5,5"/>'
        )

        elements.append(
            f'<text x="{left - 12}" y="{y + 5}" '
            f'text-anchor="end" font-size="13">'
            f'{v:.6g}</text>'
        )

    # 坐标轴
    elements.append(
        f'<line x1="{left}" y1="{top}" '
        f'x2="{left}" y2="{bottom}" stroke="black"/>'
    )

    elements.append(
        f'<line x1="{left}" y1="{bottom}" '
        f'x2="{right}" y2="{bottom}" stroke="black"/>'
    )

    # 波形曲线
    points = " ".join(
        f"{map_x(t):.3f},{map_y(v):.3f}"
        for t, v in zip(plot_times, plot_values)
    )

    elements.append(
        f'<polyline points="{points}" '
        f'fill="none" stroke="#1677cc" '
        f'stroke-width="1.5" '
        f'stroke-linejoin="round" '
        f'stroke-linecap="round"/>'
    )

    # 轴名称
    elements.append(
        f'<text x="{(left + right) / 2}" y="620" '
        f'text-anchor="middle" font-size="17">'
        f'{html.escape(x_label)}</text>'
    )

    elements.append(
        f'<text x="25" y="{(top + bottom) / 2}" '
        f'text-anchor="middle" font-size="17" '
        f'transform="rotate(-90 25 {(top + bottom) / 2})">'
        f'{html.escape(y_label)}</text>'
    )

    svg = f'''<svg xmlns="http://www.w3.org/2000/svg"
        width="{width}" height="{height}"
        viewBox="0 0 {width} {height}">
        {''.join(elements)}
    </svg>'''

    with open(path, "w", encoding="utf-8") as file:
        file.write(svg)

    return path


# ============================================================
# 9. 多实例并行仿真支持
# ============================================================

class MultisimWorker:
    """A single Multisim COM instance bound to one thread.

    Each worker initialises COM in its own thread, opens its own copy
    of the circuit file, and can run simulations independently of
    other workers.  This enables true parallel simulation when the
    Multisim COM server allows concurrent access (which it does for
    the apartment-threaded In-Process server).

    Usage::

        worker = MultisimWorker(circuit_path, output_name, duration, ...)
        worker.start()          # connect + open circuit in worker thread
        worker.set_resistors(params)
        times, values = worker.simulate(params)
        worker.shutdown()
    """

    def __init__(
        self,
        circuit_path,
        output_name,
        duration=0.01,
        sample_rate=10000.0,
        num_samples=1000,
        timeout=30,
        label="worker",
    ):
        self.circuit_path = circuit_path
        self.output_name = output_name
        self.duration = duration
        self.sample_rate = sample_rate
        self.num_samples = num_samples
        self.timeout = timeout
        self.label = label

        self._thread = None
        self._app = None
        self._circuit = None
        self._com_ready = threading.Event()
        self._init_error = None
        self._lock = threading.Lock()

    def _run(self):
        """Worker thread entry point: init COM, open circuit, signal ready."""
        import pythoncom

        pythoncom.CoInitialize()
        try:
            import win32com.client
            self._app = win32com.client.gencache.EnsureDispatch(
                "MultisimInterface.MultisimApp"
            )
            self._app.Connect()
            self._circuit = self._app.OpenFile(self.circuit_path)
            self._com_ready.set()

            # Keep the thread alive — COM calls happen via _lock
            # The thread doesn't exit until shutdown() is called.
            self._shutdown_event = threading.Event()
            self._shutdown_event.wait()

        except Exception as e:
            self._init_error = e
            self._com_ready.set()
        finally:
            if self._circuit is not None:
                try:
                    self._circuit = None
                except Exception:
                    pass
            if self._app is not None:
                try:
                    self._app = None
                except Exception:
                    pass
            pythoncom.CoUninitialize()

    def start(self):
        """Launch the worker thread and wait for COM to be ready."""
        self._thread = threading.Thread(target=self._run, name=f"multisim-{self.label}", daemon=True)
        self._thread.start()
        self._com_ready.wait(timeout=60)
        if self._init_error is not None:
            raise RuntimeError(
                "MultisimWorker {} failed to start: {}".format(
                    self.label, self._init_error
                )
            )
        if not self._com_ready.is_set():
            raise TimeoutError(
                "MultisimWorker {} timed out during COM initialisation".format(
                    self.label
                )
            )

    def simulate(self, parameters):
        """Set resistors and run one simulation.  Must be called from the
        thread that owns the COM objects, so we use the lock to serialise
        access within this worker.

        Returns (times, values).
        """
        with self._lock:
            if self._circuit is None:
                raise RuntimeError("Worker {} is not started".format(self.label))
            set_rlc_values(self._circuit, parameters)
            return get_waveform(
                self._circuit,
                self.output_name,
                duration=self.duration,
                sample_rate=self.sample_rate,
                num_samples=self.num_samples,
                timeout=self.timeout,
            )

    def set_resistors(self, parameters):
        """Set resistor values without running a simulation."""
        with self._lock:
            if self._circuit is None:
                raise RuntimeError("Worker {} is not started".format(self.label))
            set_rlc_values(self._circuit, parameters)

    @property
    def circuit(self):
        """Direct access to the circuit COM object (for SaveAs etc.)."""
        return self._circuit

    def find_resistors(self):
        """Read resistor values from this worker's circuit."""
        with self._lock:
            if self._circuit is None:
                raise RuntimeError("Worker {} is not started".format(self.label))
            return find_resistors(self._circuit)

    def get_outputs(self):
        """Read output probe names from this worker's circuit."""
        with self._lock:
            if self._circuit is None:
                raise RuntimeError("Worker {} is not started".format(self.label))
            return list(get_all_outputs(self._circuit))

    def shutdown(self):
        """Signal the worker thread to exit and release COM."""
        if hasattr(self, "_shutdown_event"):
            self._shutdown_event.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None
        self._circuit = None
        self._app = None
