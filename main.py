# -*- coding: utf-8 -*-            
# @Author: mortal_sjh
import dataclasses
import os
import platform
import re
import statistics
import subprocess
import sys
import threading
import time
import pandas as pd
from datetime import datetime
from statistics import mean

from ios_device.cli.base import InstrumentsBase
from ios_device.cli.cli import print_json
from ios_device.remote.remote_lockdown import RemoteLockdownClient
from ios_device.util.exceptions import InstrumentRPCParseError
from ios_device.util.gpu_decode import JSEvn, GRCDecodeOrder, GRCDisplayOrder, TraceData
from ios_device.util.kperf_data import kdbg_extract_all
from ios_device.util.utils import convertBytes, kperf_data, NANO_SECOND, DumpDisk, DumpNetwork, DumpMemory
from ios_device.util.utils import MOVIE_FRAME_COST
from packaging.version import Version
from pymobiledevice3 import usbmux
from pymobiledevice3.lockdown import create_using_usbmux


class TunnelManager:
    def __init__(self):
        self.start_event = threading.Event()
        self.tunnel_host = None
        self.tunnel_port = None
        self.tunnel_thread = None
        self.rp = None

    def get_tunnel(self):
        def start_tunnel():
            self.rp = subprocess.Popen([sys.executable, "-m", "pymobiledevice3", "lockdown", "start-tunnel"],
                                       stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT)
            while not self.rp.poll():
                try:
                    line = self.rp.stdout.readline().decode()
                except:
                    print("decode fail {0}".format(line))
                    continue
                line = line.strip()
                if line:
                    print(line)
                if "--rsd" in line:
                    ipv6_pattern = r'--rsd\s+(\S+)\s+'
                    port_pattern = r'\s+(\d{1,5})\b'
                    self.tunnel_host = re.search(ipv6_pattern, line).group(1)
                    self.tunnel_port = int(re.search(port_pattern, line).group(1))
                    self.start_event.set()

        self.tunnel_thread = threading.Thread(target=start_tunnel)
        self.tunnel_thread.start()
        self.start_event.wait(timeout=15)

    def close_tunnel(self):
        if self.rp:
            self.rp.terminate()
            self.rp.wait()
            self.tunnel_thread.join()


class PerformanceAnalyzer:
    def __init__(self, udid, host, port):
        self.udid = udid
        self.host = host
        self.port = port
        self.last_frame = None
        self.last_1_frame_cost, self.last_2_frame_cost, self.last_3_frame_cost = 0, 0, 0
        self.jank_count = 0
        self.big_jank_count = 0
        self.jank_time_count = 0
        self.this_mach_time_factor = 125 / 3
        self.frame_count = 0
        self.time_count = 0
        self.last_time = datetime.now().timestamp()
        self._list = []
        self.disk = DumpDisk()
        self.Network = DumpNetwork()
        self.Memory = DumpMemory()
        self.pid = None
        self.decode_key_list = []
        self.js_env: JSEvn = None
        self.display_key_list = []
        self.first_time = time.time()
        self.cpu_result_list = []
        self.memory_result_list = []
        self.disk_result_list = []
        self.network_result_list = []
        self.app_performance_result_list = []
        self.fps_result_list = []
        self.jank_time_count_All = 0
        self.time_count_all = 0

    def ios17_proc_perf(self):
        """ Get application performance data """
        max_retries = 5
        retries = 0

        while retries < max_retries:
            try:
                with RemoteLockdownClient((self.host, self.port)) as rsd:
                    with InstrumentsBase(udid=self.udid, network=False, lockdown=rsd) as rpc:
                        yield rpc
                break
            except Exception as e:
                print(f"Connection failed: {e}. Retrying...")
                retries += 1
                time.sleep(1)

    def on_callback_proc_message(self, res):
        proc_filter = ['Pid', 'Name', 'CPU', 'Memory', 'DiskReads', 'DiskWrites', 'Threads']
        process_attributes = dataclasses.make_dataclass('SystemProcessAttributes', proc_filter)
        if isinstance(res.selector, list):
            for index, row in enumerate(res.selector):
                current_time = time.time() - self.first_time
                if 'Processes' in row:
                    for _pid, process in row['Processes'].items():
                        attrs = process_attributes(*process)
                        if name and attrs.Name != name:
                            continue
                        if not attrs.CPU:
                            attrs.CPU = 0
                        if ios_version < Version('14.0'):
                            attrs.CPU = attrs.CPU * 40
                        attrs.CPU = f'{round(attrs.CPU, 2)} %'
                        attrs.Memory = convertBytes(attrs.Memory)
                        attrs.DiskReads = convertBytes(attrs.DiskReads)
                        attrs.DiskWrites = convertBytes(attrs.DiskWrites)
                        attrs_dict = attrs.__dict__
                        attrs_dict['CurrentTime'] = current_time
                        print_json(attrs_dict, format)
                        self.app_performance_result_list.append(attrs_dict)
                if 'System' in row:
                    data = dict(zip(rpc.system_attributes, row['System']))
                    memory_map = self.Memory.decode(data)
                    memory_map["CurrentTime"] = current_time
                    network_map = self.Network.decode(data)
                    network_map["CurrentTime"] = current_time
                    disk_map = self.disk.decode(data)
                    disk_map["CurrentTime"] = current_time
                    print("Memory  >>", memory_map)
                    self.memory_result_list.append(memory_map)
                    print("Network >>", network_map)
                    self.network_result_list.append(network_map)
                    print("Disk    >>", disk_map)
                    self.disk_result_list.append(disk_map)
                if "SystemCPUUsage" in row:
                    SystemCPUUsage = row["SystemCPUUsage"]
                    cpu_map = SystemCPUUsage
                    cpu_map["CurrentTime"] = current_time
                    print("CPU     >>", cpu_map)
                    self.cpu_result_list.append(cpu_map)

    def on_callback_gpu_message(self, res):
        if isinstance(res.selector, list):
            if res.selector[0] == 1:
                self.js_env.dump_trace(TraceData(*res.selector[:6]))
            elif res.selector[0] == 0:
                _data = res.selector[4]
                decode_key_list = GRCDecodeOrder.decode(_data.get(1))
                display_key_list = GRCDisplayOrder.decode(_data.get(0))
                self.js_env = JSEvn(_data.get(2), display_key_list, decode_key_list, mach_time_factor)

    def on_callback_fps_message_dump(self, res):
        if type(res.selector) is InstrumentRPCParseError:
            for args in kperf_data(res.selector.data):
                _time, code = args[0], args[7]
                if kdbg_extract_all(code) == (0x31, 0x80, 0xc6):
                    if not self.last_frame:
                        self.last_frame = _time
                    else:
                        this_frame_cost = (_time - self.last_frame) * self.this_mach_time_factor
                        if all([self.last_3_frame_cost != 0, self.last_2_frame_cost != 0, self.last_1_frame_cost != 0]):
                            if this_frame_cost > mean([self.last_3_frame_cost, self.last_2_frame_cost,
                                                       self.last_1_frame_cost]) * 2 \
                                    and this_frame_cost > MOVIE_FRAME_COST * NANO_SECOND * 2:
                                self.jank_count += 1
                                self.jank_time_count += this_frame_cost
                                if this_frame_cost > mean(
                                        [self.last_3_frame_cost, self.last_2_frame_cost, self.last_1_frame_cost]) * 3 \
                                        and this_frame_cost > MOVIE_FRAME_COST * NANO_SECOND * 3:
                                    self.big_jank_count += 1

                        self.last_3_frame_cost, self.last_2_frame_cost, self.last_1_frame_cost = \
                            (self.last_2_frame_cost, self.last_1_frame_cost, this_frame_cost)
                        self.time_count += this_frame_cost
                        self.last_frame = _time
                        self.frame_count += 1
                        self.jank_time_count_All += self.jank_time_count
                        self.time_count_all += self.time_count

                if self.time_count > NANO_SECOND:
                    result_map = {"time": datetime.now().timestamp() - self.last_time,
                                  "fps": self.frame_count / self.time_count * NANO_SECOND,
                                  "jank": self.jank_count,
                                  "big_jank": self.big_jank_count,
                                  "stutter": self.jank_time_count / self.time_count,
                                  "all_stutter": self.jank_time_count_All / self.time_count_all}
                    print(result_map)
                    self.fps_result_list.append(result_map)
                    self.jank_count = 0
                    self.big_jank_count = 0
                    self.jank_time_count = 0
                    self.frame_count = 0
                    self.time_count = 0

    def create_report(self):
        all_dict_lists = [self.app_performance_result_list, self.fps_result_list, self.memory_result_list,
                          self.network_result_list, self.disk_result_list]
        fps = [fps['fps'] for fps in self.fps_result_list if 'fps' in fps]
        average_fps = statistics.mean(fps) if fps else 0
        mem = [extract_number(app_p['Memory']) for app_p in self.app_performance_result_list if 'Memory' in app_p]
        cpu = [extract_number(app_p['CPU']) for app_p in self.app_performance_result_list if 'CPU' in app_p]
        average_mem = statistics.mean(mem) if mem else 0
        average_cpu = statistics.mean(cpu) if cpu else 0
        sheet_names = ['AppPerformance', 'fps', 'memory', 'network', 'disk']
        self.fps_result_list.append({'fps_value': average_fps})
        self.app_performance_result_list.append({'Memory_Average': f"MemoryAverage:{average_mem}",
                                                 'CPU_Average': f"CPUAverage:{average_cpu}"})
        if self.cpu_result_list.__len__() > 0:
            all_dict_lists.append(self.cpu_result_list)
            sheet_names.append("cpu")
        current_date = datetime.now().strftime('%Y%m%d_%H_%M_%S')
        with pd.ExcelWriter(f'apm_{current_date}.xlsx', engine='openpyxl') as writer:
            for dict_list, sheet_name in zip(all_dict_lists, sheet_names):
                df = pd.DataFrame(dict_list)
                df.to_excel(writer, sheet_name=sheet_name, index=False)

        print("报告创建成功！")


def run_sysmontap(rpc, callback, stop_even):
    rpc.system_attributes = rpc.device_info.sysmonSystemAttributes()
    rpc.process_attributes = ['pid', 'name', 'cpuUsage', 'physFootprint',
                              'diskBytesRead', 'diskBytesWritten', 'threadCount']
    rpc.sysmontap(callback, stopSignal=stop_even, time=1000)


def run_gpu(rpc, callback, stop_even):
    rpc.gpu_counters(callback, stopSignal=stop_even)


def run_fps(rpc, callback, stop_even):
    rpc.core_profile_session(callback, stopSignal=stop_even)


def extract_number(s):
    match = re.search(r'([\d.]+)', s)
    return float(match.group(1)) if match else 0


if __name__ == '__main__':
    if "Windows" in platform.platform():
        import ctypes

        assert ctypes.windll.shell32.IsUserAnAdmin() == 1, "必须使用管理员权限启动"
    else:
        assert os.geteuid() == 0, "必须使用sudo权限启动"
    device = usbmux.list_devices(usbmux_address=None)
    lockdown = create_using_usbmux(device[0].serial, autopair=False, connection_type=device[0].connection_type,
                                   usbmux_address=None)
    ios_version = Version(lockdown.short_info.get('ProductVersion'))
    bundle_id = "com.habby.capybara"  # 改为你要测的应用包名
    udid = lockdown.short_info.get("UniqueDeviceID")
    tunnel_manager = TunnelManager()
    tunnel_manager.get_tunnel()
    stop_even = threading.Event()
    performance_analyzer = PerformanceAnalyzer(udid, tunnel_manager.tunnel_host, tunnel_manager.tunnel_port)
    for rpc in performance_analyzer.ios17_proc_perf():
        if bundle_id:
            app = rpc.application_listing(bundle_id)
            if not app:
                print(f"not find {bundle_id}")
            name = app.get('ExecutableName')
        mach_time_info = rpc.device_info.machTimeInfo()
        mach_time_factor = mach_time_info[1] / mach_time_info[2]
        sysmontap_thread = threading.Thread(target=run_sysmontap,
                                            args=(rpc, performance_analyzer.on_callback_proc_message, stop_even))
        gpu_thread = threading.Thread(target=run_gpu,
                                      args=(rpc, performance_analyzer.on_callback_gpu_message, stop_even))
        fps_thread = threading.Thread(target=run_fps,
                                      args=(rpc, performance_analyzer.on_callback_fps_message_dump, stop_even))
        sysmontap_thread.start()
        time.sleep(0.6)
        fps_thread.start()
        # gpu_thread.start()  # 先不要启这个，这个貌似有点问题，这个用来抓gpu的

        # 设置运行时间，例如3分钟后停止
        time.sleep(15)  # 先在这里弄一下demo，自己改一下时间
        stop_even.set()

        sysmontap_thread.join()
        fps_thread.join()
        tunnel_manager.close_tunnel()
        # gpu_thread.join()   # 先不要启这个，这个貌似有点问题，这个用来抓gpu的

        performance_analyzer.create_report()
