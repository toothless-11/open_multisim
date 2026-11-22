"""Open Multisim, keep only the first successful window, kill the rest.
Retry the whole batch if all five failed."""

import os
import subprocess
import time


POLL_INTERVAL_SECONDS = 1.0
WINDOW_SETTLE_SECONDS = 20.0
WINDOW_COUNT = 5
FAILURE_STRIKES_REQUIRED = 2
MAX_ROUNDS = 5
RETRY_DELAY_SECONDS = 3.0


def find_multisim_executable():
    candidates = []
    try:
        import winreg
        for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for path in (
                r"SOFTWARE\National Instruments\Circuit Design Suite",
                r"SOFTWARE\WOW6432Node\National Instruments\Circuit Design Suite",
            ):
                try:
                    with winreg.OpenKey(root, path) as key:
                        index = 0
                        while True:
                            try:
                                name, value, _ = winreg.EnumValue(key, index)
                                index += 1
                                if "path" in name.lower() or "install" in name.lower():
                                    candidates.append(os.path.join(str(value), "Multisim.exe"))
                            except OSError:
                                break
                except OSError:
                    pass
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\Multisim.exe",
        ) as key:
            candidates.append(winreg.QueryValue(key, None))
    except (ImportError, OSError):
        pass

    for drive in ("C:\\", "D:\\", "E:\\"):
        base = os.path.join(drive, "Program Files")
        if os.path.isdir(base):
            for root, _, files in os.walk(base):
                if "Multisim.exe" in files:
                    candidates.append(os.path.join(root, "Multisim.exe"))

    candidates.extend([
        r"C:\Program Files\National Instruments\Circuit Design Suite 14.3\Multisim.exe",
        r"C:\Program Files (x86)\National Instruments\Circuit Design Suite 14.3\Multisim.exe",
        r"C:\Program Files\National Instruments\Circuit Design Suite 14.2\Multisim.exe",
        r"C:\Program Files (x86)\National Instruments\Circuit Design Suite 14.2\Multisim.exe",
    ])

    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return os.path.abspath(candidate)
    return None


def launch_multisim(executable):
    return subprocess.Popen([executable], shell=False)


def get_process_tree_pids(root_pid):
    pids = {root_pid}
    try:
        import wmi
    except ImportError:
        return pids
    try:
        c = wmi.WMI()
        children_map = {}
        for proc in c.Win32_Process():
            children_map.setdefault(proc.ParentProcessId, []).append(proc.ProcessId)
        stack = [root_pid]
        while stack:
            parent = stack.pop()
            for child in children_map.get(parent, []):
                if child not in pids:
                    pids.add(child)
                    stack.append(child)
    except Exception:
        pass
    return pids


def get_child_texts(hwnd):
    import win32gui
    out = []

    def add(h, _):
        try:
            t = win32gui.GetWindowText(h)
            if t and t.strip():
                out.append((win32gui.GetClassName(h), t.strip()))
        except Exception:
            pass

    try:
        win32gui.EnumChildWindows(hwnd, add, None)
    except Exception:
        pass
    return out


def inspect_windows(pids):
    import win32gui
    import win32process

    has_error_dialog = False
    main_window_disabled = False
    details = []

    def enum(hwnd, _):
        nonlocal has_error_dialog, main_window_disabled
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if pid not in pids:
                return
            cls = win32gui.GetClassName(hwnd)
            title = (win32gui.GetWindowText(hwnd) or "").strip()

            if cls == "Multisim14.3":
                if not win32gui.IsWindowEnabled(hwnd):
                    main_window_disabled = True
                    details.append("主窗口被禁用: {}".format(title))
                return

            if cls == "#32770":
                children = get_child_texts(hwnd)
                title_l = title.lower()
                looks_like_error = (
                    title_l in ("multisim", "")
                    and len(children) <= 1
                    and any("ok" in (t or "").lower() for _, t in children)
                )
                if looks_like_error:
                    has_error_dialog = True
                    details.append(
                        "错误对话框: title={!r} children={}".format(title, children)
                    )
        except Exception:
            pass

    win32gui.EnumWindows(enum, None)
    return has_error_dialog, main_window_disabled, details


def kill_process_tree(pid):
    try:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except Exception:
        pass


def run_one_round(executable):
    """Launch WINDOW_COUNT instances.
    Returns (all_processes, kept_process_or_None).
    Only the first successful process is kept; other successful ones are killed.
    """
    processes = []
    for index in range(WINDOW_COUNT):
        print("正在打开第 {} 个 Multisim 窗口...".format(index + 1))
        try:
            processes.append(launch_multisim(executable))
        except Exception as error:
            print("第 {} 个窗口启动失败：{}".format(index + 1, error))

    failed_pids = set()
    strikes = {p.pid: 0 for p in processes}

    deadline = time.monotonic() + WINDOW_SETTLE_SECONDS
    while time.monotonic() < deadline:
        time.sleep(POLL_INTERVAL_SECONDS)

        for process in processes:
            if process.poll() is not None or process.pid in failed_pids:
                continue

            tree_pids = get_process_tree_pids(process.pid)
            has_error, main_disabled, details = inspect_windows(tree_pids)

            if has_error and main_disabled:
                strikes[process.pid] += 1
                print(
                    "PID {} 第 {} 次检测到数据库错误对话框（{}）".format(
                        process.pid, strikes[process.pid], "; ".join(details)
                    )
                )
                if strikes[process.pid] >= FAILURE_STRIKES_REQUIRED:
                    print("终止失败进程树：{}".format(process.pid))
                    kill_process_tree(process.pid)
                    failed_pids.add(process.pid)
            else:
                strikes[process.pid] = 0

    survivors = [
        p for p in processes
        if p.pid not in failed_pids and p.poll() is None
    ]

    kept = None
    if survivors:
        # 按启动顺序取第一个成功的
        kept = survivors[0]
        print("保留第一个成功窗口：PID {}".format(kept.pid))
        for extra in survivors[1:]:
            print("关闭多余成功窗口：PID {}".format(extra.pid))
            kill_process_tree(extra.pid)

    print(
        "本轮检查完成：成功 {} 个，保留 1 个，终止 {} 个失败实例。".format(
            len(survivors), len(processes) - len(survivors)
        )
    )
    return processes, kept


def open_multisim():
    executable = find_multisim_executable()
    if executable is None:
        raise FileNotFoundError(
            "未找到 Multisim.exe，请确认已安装 NI Circuit Design Suite。"
        )

    for round_index in range(1, MAX_ROUNDS + 1):
        print("=" * 60)
        print("第 {} 轮启动".format(round_index))
        print("=" * 60)
        processes, kept = run_one_round(executable)

        if kept is not None:
            print("最终保留窗口 PID：{}".format(kept.pid))
            return kept

        print("本轮 {} 个窗口全部失败。清理残留进程后重试...".format(WINDOW_COUNT))
        for process in processes:
            if process.poll() is None:
                kill_process_tree(process.pid)
        time.sleep(RETRY_DELAY_SECONDS)

    print("已达到最大重试轮数 {}，仍未获得成功窗口。".format(MAX_ROUNDS))
    return None


if __name__ == "__main__":
    open_multisim()