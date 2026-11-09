#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ESXi 监控平台（本地桌面版）
==========================
一个纯本地运行的 Python 桌面程序，用于监控：
  1. ESXi / vSphere 主机的硬件健康（传感器、风扇、电源、CPU/内存健康状态）
  2. 主机与虚拟机的资源利用率（CPU、内存、存储）
  3. 虚拟机健康状态（电源状态、VMware Tools、整体告警、资源占用）

特性：
  - 所有对接均手动完成，可保存多个 ESXi / vCenter 的连接信息（含账号密码）
  - 连接信息保存在本地 JSON 文件中（密码做简单编码，非明文）
  - 后台线程定时刷新，界面不卡顿
  - 仅依赖 pyVmomi + 自带 tkinter

运行前请先安装依赖：
    pip install pyvmomi

直接运行（推荐双击启动器，无黑色命令行窗口）：
    python esxi_monitor.py
    （双击 esxi_monitor.py 时会自动改用 pythonw 隐藏黑框；
     若当前 Python 没装 pyVmomi，会自动寻找装了的 Python 重启）

更省事：直接双击同目录的「启动ESXi监控.vbs」即可。
"""

import os
import sys
import json
import base64
import threading
import ssl
import time
import calendar
import socket

import tkinter as tk
from tkinter import ttk, messagebox, simpledialog

try:
    from pyVim.connect import SmartConnect, Disconnect
    from pyVmomi import vim
    HAS_PYVMOMI = True
except Exception:  # pragma: no cover - 依赖缺失时仍可打开界面并提示
    HAS_PYVMOMI = False


# ==============================================================
# 1) 配置存储：保存 ESXi / vSphere 连接信息
# ==============================================================
class ConfigStore:
    """管理本地保存的多个连接配置。

    密码以 base64 简单编码存储，仅用于避免“明文一眼可见”，
    并非加密。请勿在不可信机器上保存生产环境凭据。
    """

    def __init__(self, path=None):
        here = os.path.dirname(os.path.abspath(__file__))
        self.path = path or os.path.join(here, "esxi_connections.json")
        self.connections = []
        self.load()

    # ---- 密码简单编码/解码 ----
    @staticmethod
    def _encode(s):
        if s is None:
            return ""
        return base64.b64encode(s.encode("utf-8")).decode("ascii")

    @staticmethod
    def _decode(s):
        if not s:
            return ""
        try:
            return base64.b64decode(s.encode("ascii")).decode("utf-8")
        except Exception:
            return s

    def load(self):
        try:
            if os.path.exists(self.path):
                with open(self.path, "r", encoding="utf-8") as f:
                    self.connections = json.load(f)
            else:
                self.connections = []
        except Exception:
            self.connections = []

    def save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.connections, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print("保存连接配置失败:", e)

    def add(self, conn):
        self.connections.append(conn)
        self.save()

    def remove(self, idx):
        if 0 <= idx < len(self.connections):
            self.connections.pop(idx)
            self.save()

    def update(self, idx, conn):
        if 0 <= idx < len(self.connections):
            self.connections[idx] = conn
            self.save()


# ==============================================================
# 2) vSphere 数据采集层
# ==============================================================
class VSphereClient:
    """封装与 ESXi / vCenter 的连接与数据抓取。"""

    STATE_ORDER = {"red": 4, "yellow": 3, "green": 2, "gray": 1, "unknown": 0}

    def __init__(self, host, username, password, port=443, verify_ssl=False):
        self.host = host
        self.username = username
        self.password = password
        self.port = int(port)
        self.verify_ssl = bool(verify_ssl)
        self.timeout = 20  # 连接 / 单次请求超时（秒），避免不可达时无限阻塞
        self.si = None
        self.connected = False
        # moid -> vim.VirtualMachine 对象映射，供后续开关机/克隆/控制台等操作复用
        self.vm_by_moid = {}

    def connect(self):
        if not HAS_PYVMOMI:
            raise RuntimeError(
                "未安装 pyVmomi，请先执行：pip install pyvmomi"
            )
        # 设置全局 socket 超时，避免主机不可达 / 端口被防火墙过滤 / SSL 握手卡死时
        # SmartConnect 无限阻塞（表现为界面一直“正在连接”）
        prev_timeout = socket.getdefaulttimeout()
        socket.setdefaulttimeout(self.timeout)
        try:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE if not self.verify_ssl else ssl.CERT_REQUIRED

            self.si = SmartConnect(
                host=self.host,
                user=self.username,
                pwd=self.password,
                port=self.port,
                sslContext=context,
            )
            if not self.si:
                raise RuntimeError("无法连接到 %s（返回为空）" % self.host)
            self.connected = True
        finally:
            # 恢复全局超时设置，避免影响其它网络操作
            socket.setdefaulttimeout(prev_timeout)

    def disconnect(self):
        if self.si:
            try:
                Disconnect(self.si)
            except Exception:
                pass
        self.connected = False
        self.si = None

    # ---- 小工具 ----
    @staticmethod
    def _label(obj):
        """取 ElementDescription 的 label 文本。"""
        if obj is None:
            return ""
        if hasattr(obj, "label"):
            return obj.label or ""
        return str(obj)

    @staticmethod
    def _health_key(state):
        if state is None:
            return "unknown"
        key = getattr(state, "key", None)
        return key if key else "unknown"

    @staticmethod
    def _fmt_bytes(num_bytes):
        try:
            b = float(num_bytes)
        except Exception:
            return "0 B"
        units = ["B", "KB", "MB", "GB", "TB", "PB"]
        i = 0
        while b >= 1024 and i < len(units) - 1:
            b /= 1024.0
            i += 1
        return "%.1f %s" % (b, units[i])

    @staticmethod
    def _fmt_uptime(seconds):
        try:
            s = int(seconds)
        except Exception:
            return "—"
        d, rem = divmod(s, 86400)
        h, rem = divmod(rem, 3600)
        m, _ = divmod(rem, 60)
        if d > 0:
            return "%d 天 %d 小时 %d 分" % (d, h, m)
        return "%d 小时 %d 分" % (h, m)

    @staticmethod
    def _compute_uptime(boot_time):
        """根据开机时间计算运行时长（秒）。bootTime 通常为 UTC 时间。"""
        if not boot_time:
            return 0
        try:
            if boot_time.tzinfo is not None:
                epoch = boot_time.timestamp()
            else:
                epoch = calendar.timegm(boot_time.timetuple())
            return max(0, int(time.time() - epoch))
        except Exception:
            return 0

    @staticmethod
    def _sh_type(key):
        """把系统健康组件的 key 映射为中文分类。"""
        k = (key or "").lower()
        if "cpu" in k:
            return "CPU"
        if "mem" in k:
            return "内存"
        if "stor" in k or "disk" in k:
            return "存储/硬盘"
        if "temp" in k:
            return "温度"
        if "power" in k or "batt" in k:
            return "电源"
        if "fan" in k:
            return "风扇"
        if "sensor" in k:
            return "传感器"
        return "系统组件"

    @staticmethod
    def _sensor_type(label):
        """把传感器类型描述映射为中文分类。"""
        t = (label or "").lower()
        if "temp" in t:
            return "温度"
        if "fan" in t:
            return "风扇"
        if "volt" in t:
            return "电压"
        if "current" in t:
            return "电流"
        if "power" in t:
            return "功率"
        return label or "传感器"

    def get_data(self):
        """抓取并返回结构化监控数据。失败时抛出异常。"""
        if not self.connected or self.si is None:
            self.connect()

        content = self.si.RetrieveContent()

        # ---------- 主机 ----------
        host_view = content.viewManager.CreateContainerView(
            content.rootFolder, [vim.HostSystem], True
        )
        hosts_objs = host_view.view

        host_cpu_mhz_per_core = None
        hosts = []
        for host in hosts_objs:
            try:
                hosts.append(self._collect_host(host))
                if host_cpu_mhz_per_core is None:
                    hw = host.summary.hardware
                    if hw and hw.cpuMhz:
                        host_cpu_mhz_per_core = hw.cpuMhz
            except Exception as e:
                hosts.append({
                    "name": getattr(host, "name", "未知主机"),
                    "error": "采集失败: %s" % e,
                })

        # ---------- 虚拟机 ----------
        vm_view = content.viewManager.CreateContainerView(
            content.rootFolder, [vim.VirtualMachine], True
        )
        vms = []
        self.vm_by_moid = {}
        for vm in vm_view.view:
            try:
                v = self._collect_vm(vm, host_cpu_mhz_per_core)
                v["moid"] = vm._moId
                self.vm_by_moid[vm._moId] = vm
                vms.append(v)
            except Exception as e:
                vms.append({
                    "name": getattr(vm, "name", "未知虚拟机"),
                    "moid": getattr(vm, "_moId", ""),
                    "error": "采集失败: %s" % e,
                })
        vm_view.Destroy()

        # ---------- 存储（跨主机去重） ----------
        datastores = []
        seen = set()
        for host in hosts_objs:
            try:
                for ds in host.datastore:
                    name = ds.summary.name
                    if name in seen:
                        continue
                    seen.add(name)
                    datastores.append(self._collect_datastore(ds))
            except Exception:
                pass

        host_view.Destroy()

        return {
            "hosts": hosts,
            "vms": vms,
            "datastores": datastores,
            "collect_time": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    # ---- 单台主机采集 ----
    def _collect_host(self, host):
        summary = host.summary
        hw = summary.hardware
        q = summary.quickStats
        runtime = host.runtime

        # 硬件健康（CPU / 内存 / 存储 / 温度 / 风扇 / 电源 等）
        sensors = []
        overall = "unknown"
        try:
            hs = runtime.healthSystemRuntime
            hsi = getattr(hs, "hardwareStatusInfo", None) if hs else None
            if hsi is not None:
                # 系统级组件健康（CPU / 内存 / 存储 / 温度 / 电源 / 风扇 ...）
                for sh in (getattr(hsi, "systemHealthInfo", None) or []):
                    st = self._health_key(sh.healthState)
                    sensors.append({
                        "name": self._label(sh.label) or sh.key,
                        "type": self._sh_type(sh.key),
                        "state": st,
                        "reading": "",
                    })
                    overall = self._worse(overall, st)

                # 传感器读数（温度 / 风扇 / 电压 ...）
                for s in (getattr(hsi, "sensorInfo", None) or []):
                    st = self._health_key(s.healthState)
                    reading = ""
                    if s.currentReading is not None:
                        mod = s.unitModifier if s.unitModifier else 0
                        reading = "%s %s" % (
                            s.currentReading * (10 ** mod),
                            self._label(s.baseUnits),
                        )
                    sensors.append({
                        "name": self._label(s.name) or s.key,
                        "type": self._sensor_type(self._label(s.sensorType)),
                        "state": st,
                        "reading": reading,
                    })
                    overall = self._worse(overall, st)

                # CPU 封装健康
                for c in (getattr(hsi, "cpuStatusInfo", None) or []):
                    st = self._health_key(c.healthState)
                    sensors.append({
                        "name": self._label(c.name) or c.key,
                        "type": "CPU",
                        "state": st,
                        "reading": "",
                    })
                    overall = self._worse(overall, st)

                # 内存健康
                for m in (getattr(hsi, "memoryStatusInfo", None) or []):
                    st = self._health_key(m.healthState)
                    sensors.append({
                        "name": self._label(m.name) or m.key,
                        "type": "内存",
                        "state": st,
                        "reading": "",
                    })
                    overall = self._worse(overall, st)
        except Exception:
            pass

        # 若主机未提供任何健康数据，给出明确提示而不是空白
        if not sensors:
            sensors.append({
                "name": "未获取到硬件健康数据（该主机可能未启用硬件健康服务，或无可用传感器）",
                "type": "提示",
                "state": "unknown",
                "reading": "",
            })

        # 资源利用率
        cpu_total_mhz = 0
        cpu_usage = q.overallCpuUsage or 0
        mem_total_mb = 0.0
        mem_usage = q.overallMemoryUsage or 0
        if hw:
            cpu_total_mhz = (hw.cpuMhz or 0) * (hw.numCpuCores or 0)
            mem_total_mb = (hw.memorySize or 0) / (1024.0 * 1024.0)

        cpu_pct = (cpu_usage / cpu_total_mhz * 100.0) if cpu_total_mhz else 0.0
        mem_pct = (mem_usage / mem_total_mb * 100.0) if mem_total_mb else 0.0

        return {
            "name": host.name,
            "vendor": getattr(hw, "vendor", "") if hw else "",
            "model": getattr(hw, "model", "") if hw else "",
            "uuid": getattr(hw, "uuid", "") if hw else "",
            "power_state": getattr(runtime, "powerState", "") or "",
            "connection_state": getattr(runtime, "connectionState", "") or "",
            "boot_time": str(getattr(summary.runtime, "bootTime", "") or ""),
            "uptime": self._compute_uptime(getattr(summary.runtime, "bootTime", None)),
            "cpu_model": getattr(hw, "cpuModel", "") if hw else "",
            "cpu_cores": getattr(hw, "numCpuCores", 0) if hw else 0,
            "cpu_threads": getattr(hw, "numCpuThreads", 0) if hw else 0,
            "cpu_total_mhz": cpu_total_mhz,
            "cpu_usage_mhz": cpu_usage,
            "cpu_pct": round(cpu_pct, 1),
            "mem_total_mb": round(mem_total_mb, 1),
            "mem_usage_mb": mem_usage,
            "mem_pct": round(mem_pct, 1),
            "overall_health": overall,
            "sensors": sensors,
        }

    # ---- 单台虚拟机采集 ----
    def _collect_vm(self, vm, host_cpu_mhz_per_core):
        config = vm.summary.config
        qs = vm.summary.quickStats
        runtime = vm.runtime
        guest = vm.guest

        num_cpu = config.numCpu or 0
        mem_size_mb = config.memorySizeMB or 0
        cpu_mhz = qs.overallCpuUsage or 0

        alloc_mhz = num_cpu * (host_cpu_mhz_per_core or 0)
        cpu_pct = (cpu_mhz / alloc_mhz * 100.0) if alloc_mhz else 0.0
        guest_mem = qs.guestMemoryUsage or 0
        mem_pct = (guest_mem / mem_size_mb * 100.0) if mem_size_mb else 0.0

        return {
            "name": config.name,
            "power_state": getattr(runtime, "powerState", "") or "",
            "guest_state": getattr(guest, "guestState", "") or "",
            "tools_status": getattr(guest, "toolsStatus", "") or "",
            "overall_status": getattr(vm.summary, "overallStatus", "") or "",
            "num_cpu": num_cpu,
            "mem_size_mb": mem_size_mb,
            "cpu_mhz": cpu_mhz,
            "cpu_pct": round(cpu_pct, 1),
            "guest_mem_mb": guest_mem,
            "host_mem_mb": qs.hostMemoryUsage or 0,
            "mem_pct": round(mem_pct, 1),
            "uptime": qs.uptimeSeconds or 0,
            "ip": getattr(guest, "ipAddress", "") or "",
            "os": getattr(guest, "guestFullName", "") or config.guestFullName or "",
        }

    # ---- 单数据存储采集 ----
    def _collect_datastore(self, ds):
        s = ds.summary
        cap = s.capacity or 0
        free = s.freeSpace or 0
        used = cap - free
        used_pct = (used / cap * 100.0) if cap else 0.0
        return {
            "name": s.name,
            "type": s.type or "",
            "capacity_gb": round(cap / (1024.0 ** 3), 1),
            "free_gb": round(free / (1024.0 ** 3), 1),
            "used_pct": round(used_pct, 1),
            "accessible": bool(s.accessible),
        }

    # ---- 虚拟机操作（开关机 / 挂起 / 重启 / 克隆）----
    @staticmethod
    def _wait_task(task, timeout=180):
        """轮询等待任务完成；超时则视为已提交（后台继续）；失败抛 RuntimeError。"""
        import time
        start = time.time()
        while time.time() - start < timeout:
            try:
                info = task.info
            except Exception:
                return
            state = getattr(info, "state", None)
            if state == "success":
                return
            if state == "error":
                err = getattr(info, "error", None)
                msg = getattr(err, "localizedMessage", None) or str(err)
                raise RuntimeError("任务失败: %s" % msg)
            time.sleep(2)

    def vm_action(self, action, moid, new_name=None):
        """对指定虚拟机执行操作。action: on/off/suspend/reset/clone。
        返回 (ok, msg)；应在后台线程中调用。"""
        vm = self.vm_by_moid.get(moid)
        if vm is None:
            return False, "未找到该虚拟机（可能已断开，请刷新后重试）"
        try:
            if action == "on":
                if vm.runtime.powerState == "poweredOn":
                    return True, "该虚拟机已经是开机状态"
                self._wait_task(vm.PowerOnVM_Task(), 180)
                return True, "已发送开机指令"
            if action == "off":
                if vm.runtime.powerState == "poweredOff":
                    return True, "该虚拟机已经是关机状态"
                self._wait_task(vm.PowerOffVM_Task(), 180)
                return True, "已发送关机指令"
            if action == "suspend":
                if vm.runtime.powerState != "poweredOn":
                    return False, "只有运行中的虚拟机才能挂起"
                self._wait_task(vm.SuspendVM_Task(), 180)
                return True, "已发送挂起指令"
            if action == "reset":
                if vm.runtime.powerState != "poweredOn":
                    return False, "只有运行中的虚拟机才能重启"
                self._wait_task(vm.ResetVM_Task(), 180)
                return True, "已发送重启指令"
            if action == "clone":
                if not new_name:
                    return False, "未提供新虚拟机名称"
                reloc = vim.VirtualMachineRelocateSpec()
                try:
                    reloc.pool = vm.resourcePool
                except Exception:
                    pass
                try:
                    if vm.datastore:
                        reloc.datastore = vm.datastore[0]
                except Exception:
                    pass
                spec = vim.VirtualMachineCloneSpec(
                    location=reloc, powerOn=False, template=False)
                task = vm.CloneVM_Task(folder=vm.parent, name=new_name, spec=spec)
                self._wait_task(task, 300)
                return True, "克隆任务已提交：%s" % new_name
            return False, "未知操作: %s" % action
        except Exception as e:
            return False, "操作失败: %s" % e

    def console_tickets(self, moid):
        """获取虚拟机控制台访问方式，返回 dict：
           vmrc_url: VMRC 协议地址（需本机安装 VMware Remote Console）
           web_url:  ESXi 内嵌主机客户端 Web 控制台地址（浏览器打开）
        """
        vm = self.vm_by_moid.get(moid)
        if vm is None:
            return None
        result = {}
        try:
            ticket = vm.AcquireTicket("vmrc")
            host = getattr(ticket, "host", None) or self.host
            port = getattr(ticket, "port", None) or 902
            url = "vmrc://%s@%s:%s/?moid=%s" % (ticket.ticket, host, port, moid)
            tp = getattr(ticket, "sslThumbprint", None)
            if tp:
                url += "&thumbprint=%s" % tp
            result["vmrc_url"] = url
        except Exception as e:
            result["vmrc_error"] = "获取 VMRC 票据失败: %s" % e
        result["web_url"] = "https://%s/ui/webconsole.html?vmId=%s" % (self.host, moid)
        return result

    @classmethod
    def _worse(cls, a, b):
        if cls.STATE_ORDER.get(b, 0) > cls.STATE_ORDER.get(a, 0):
            return b
        return a


# ==============================================================
# 3) Tkinter 监控界面
# ==============================================================
class ConnectionDialog(simpledialog.Dialog):
    """新建/编辑连接的弹窗。"""

    def __init__(self, parent, title, conn=None):
        self.conn = conn or {
            "name": "", "host": "", "port": 443,
            "username": "", "password": "", "verify_ssl": False,
        }
        self.result = None
        super().__init__(parent, title)

    def body(self, master):
        ttk.Label(master, text="连接名称:").grid(row=0, column=0, sticky="e", padx=4, pady=4)
        ttk.Label(master, text="主机地址:").grid(row=1, column=0, sticky="e", padx=4, pady=4)
        ttk.Label(master, text="端口:").grid(row=2, column=0, sticky="e", padx=4, pady=4)
        ttk.Label(master, text="用户名:").grid(row=3, column=0, sticky="e", padx=4, pady=4)
        ttk.Label(master, text="密码:").grid(row=4, column=0, sticky="e", padx=4, pady=4)
        ttk.Label(master, text="校验 SSL:").grid(row=5, column=0, sticky="e", padx=4, pady=4)

        self.e_name = ttk.Entry(master, width=34)
        self.e_host = ttk.Entry(master, width=34)
        self.e_port = ttk.Entry(master, width=34)
        self.e_user = ttk.Entry(master, width=34)
        self.e_pwd = ttk.Entry(master, width=34, show="*")
        self.var_ssl = tk.BooleanVar()

        self.e_name.grid(row=0, column=1, padx=4, pady=4)
        self.e_host.grid(row=1, column=1, padx=4, pady=4)
        self.e_port.grid(row=2, column=1, padx=4, pady=4)
        self.e_user.grid(row=3, column=1, padx=4, pady=4)
        self.e_pwd.grid(row=4, column=1, padx=4, pady=4)
        self.c_ssl = ttk.Checkbutton(master, text="（默认关闭，自签名证书可勾选关闭校验）", variable=self.var_ssl)
        self.c_ssl.grid(row=5, column=1, padx=4, pady=4, sticky="w")

        self.e_name.insert(0, self.conn.get("name", ""))
        self.e_host.insert(0, self.conn.get("host", ""))
        self.e_port.insert(0, str(self.conn.get("port", 443)))
        self.e_user.insert(0, self.conn.get("username", ""))
        self.e_pwd.insert(0, ConfigStore._decode(self.conn.get("password", "")))
        self.var_ssl.set(bool(self.conn.get("verify_ssl", False)))

        return self.e_name

    def apply(self):
        self.result = {
            "name": self.e_name.get().strip(),
            "host": self.e_host.get().strip(),
            "port": int(self.e_port.get().strip() or 443),
            "username": self.e_user.get().strip(),
            "password": ConfigStore._encode(self.e_pwd.get()),
            "verify_ssl": self.var_ssl.get(),
        }


class ESXiMonitorApp:
    STATE_COLORS = {
        "green": "#2e7d32",
        "yellow": "#f9a825",
        "red": "#c62828",
        "gray": "#757575",
        "unknown": "#757575",
    }
    STATE_LABELS = {
        "green": "正常", "yellow": "警告", "red": "严重",
        "gray": "未知", "unknown": "未知",
    }

    def __init__(self, root):
        self.root = root
        self.root.title("ESXi 监控平台")
        self.root.geometry("1100x720")

        self.config = ConfigStore()
        self.client = None
        self.current_conn = None
        self.last_data = None
        self.last_host_idx = 0
        self.auto_refresh = False
        self.auto_timer = None
        self.connect_done = True  # 连接是否已完成（用于看门狗判断）
        # 虚拟机操作：当前选中的虚拟机
        self.selected_vm_moid = None
        self.selected_vm_name = ""
        self.selected_card = None  # 卡片模式下当前高亮的卡片控件
        self.vm_tree_moids = {}

        # 资源利用率进度条样式（根据百分比配色）
        self._build_styles()

        self._build_layout()
        self._refresh_conn_list()

        if not HAS_PYVMOMI:
            messagebox.showwarning(
                "缺少依赖",
                "未检测到 pyVmomi。请先安装：\n\n    pip install pyvmomi\n\n"
                "安装后即可连接并监控 ESXi / vSphere。",
            )

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------------- 样式 ----------------
    def _build_styles(self):
        style = ttk.Style()
        # 利用率进度条：绿/黄/红
        for name, color in (("green", "#2e7d32"),
                            ("yellow", "#f9a825"),
                            ("red", "#c62828")):
            style.configure("pb_%s.Horizontal.TProgressbar" % name,
                            troughcolor="#eeeeee", background=color)
        # 健康状态标签样式
        style.configure("state.TLabel", font=("Microsoft YaHei", 10, "bold"))

    # ---------------- 布局 ----------------
    def _build_layout(self):
        # 顶部工具栏
        top = ttk.Frame(self.root)
        top.pack(side=tk.TOP, fill=tk.X, padx=6, pady=4)
        ttk.Label(top, text="主机:").pack(side=tk.LEFT)
        self.host_var = tk.StringVar()
        self.host_combo = ttk.Combobox(top, textvariable=self.host_var, width=28, state="readonly")
        self.host_combo.pack(side=tk.LEFT, padx=4)
        self.host_combo.bind("<<ComboboxSelected>>", self._on_host_selected)

        self.btn_refresh = ttk.Button(top, text="刷新", command=self.refresh, state="disabled")
        self.btn_refresh.pack(side=tk.LEFT, padx=4)

        self.var_auto = tk.BooleanVar()
        self.c_auto = ttk.Checkbutton(top, text="自动刷新", variable=self.var_auto,
                                      command=self._toggle_auto)
        self.c_auto.pack(side=tk.LEFT, padx=4)
        ttk.Label(top, text="间隔(秒):").pack(side=tk.LEFT)
        self.e_interval = ttk.Entry(top, width=6)
        self.e_interval.insert(0, "30")
        self.e_interval.pack(side=tk.LEFT, padx=2)

        # 主体：左侧连接管理 + 右侧监控区
        body = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        body.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=6, pady=4)

        # 左侧连接管理
        left = ttk.LabelFrame(body, text="保存的连接", width=240)
        self.conn_list = tk.Listbox(left, height=20)
        self.conn_list.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)
        self.conn_list.bind("<Double-1>", lambda e: self.connect_selected())
        btn_frame = ttk.Frame(left)
        btn_frame.pack(fill=tk.X, padx=6, pady=4)
        ttk.Button(btn_frame, text="新建", command=self.new_connection).pack(side=tk.LEFT, expand=True, fill=tk.X)
        ttk.Button(btn_frame, text="编辑", command=self.edit_connection).pack(side=tk.LEFT, expand=True, fill=tk.X)
        ttk.Button(btn_frame, text="删除", command=self.delete_connection).pack(side=tk.LEFT, expand=True, fill=tk.X)
        ttk.Button(left, text="▶ 连接选中", command=self.connect_selected).pack(fill=tk.X, padx=6, pady=4)
        body.add(left, weight=0)

        # 右侧监控区
        right = ttk.Frame(body)
        body.add(right, weight=1)

        self.notebook = ttk.Notebook(right)
        self.notebook.pack(fill=tk.BOTH, expand=True)

        self._build_overview_tab()
        self._build_hardware_tab()
        self._build_host_tab()
        self._build_vm_tab()
        self._build_storage_tab()

        # 底部状态栏
        self.status_var = tk.StringVar(value="就绪。请先在左侧新建并连接一个 ESXi / vCenter。")
        self.status_bar = ttk.Label(self.root, textvariable=self.status_var,
                                    relief=tk.SUNKEN, anchor=tk.W)
        self.status_bar.pack(side=tk.BOTTOM, fill=tk.X)

    # ---- 概览 ----
    def _build_overview_tab(self):
        f = ttk.Frame(self.notebook)
        self.notebook.add(f, text="概览")
        self.ov_labels = {}
        rows = [
            ("主机名", "name"), ("厂商/型号", "model"), ("电源状态", "power_state"),
            ("连接状态", "connection_state"), ("运行时长", "uptime"),
            ("CPU 使用率", "cpu_pct"), ("内存使用率", "mem_pct"),
            ("虚拟机总数", "vm_total"), ("开机虚拟机", "vm_on"),
            ("存储数量", "ds_total"),
        ]
        for i, (lab, key) in enumerate(rows):
            ttk.Label(f, text=lab + ":", anchor="e", width=16).grid(
                row=i, column=0, sticky="e", padx=8, pady=3)
            v = ttk.Label(f, text="—", anchor="w")
            v.grid(row=i, column=1, sticky="w", padx=8, pady=3)
            self.ov_labels[key] = v

        # CPU / 内存条
        ttk.Label(f, text="CPU:", anchor="e", width=16).grid(row=len(rows), column=0, sticky="e", padx=8, pady=3)
        self.ov_cpu_bar = ttk.Progressbar(f, maximum=100, length=300)
        self.ov_cpu_bar.grid(row=len(rows), column=1, sticky="w", padx=8, pady=3)
        ttk.Label(f, text="内存:", anchor="e", width=16).grid(row=len(rows) + 1, column=0, sticky="e", padx=8, pady=3)
        self.ov_mem_bar = ttk.Progressbar(f, maximum=100, length=300)
        self.ov_mem_bar.grid(row=len(rows) + 1, column=1, sticky="w", padx=8, pady=3)

    # ---- 硬件健康 ----
    def _build_hardware_tab(self):
        f = ttk.Frame(self.notebook)
        self.notebook.add(f, text="硬件健康")
        head = ttk.Frame(f)
        head.pack(fill=tk.X, padx=6, pady=4)
        ttk.Label(head, text="总体健康:").pack(side=tk.LEFT)
        self.hw_overall = ttk.Label(head, text="—", style="state.TLabel")
        self.hw_overall.pack(side=tk.LEFT, padx=6)

        cols = ("名称", "类型", "状态", "读数")
        self.hw_tree = ttk.Treeview(f, columns=cols, show="headings", height=20)
        for c in cols:
            self.hw_tree.heading(c, text=c)
        self.hw_tree.column("名称", width=240)
        self.hw_tree.column("类型", width=120)
        self.hw_tree.column("状态", width=80)
        self.hw_tree.column("读数", width=140)
        self.hw_tree.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

    # ---- 主机资源 ----
    def _build_host_tab(self):
        f = ttk.Frame(self.notebook)
        self.notebook.add(f, text="主机资源")
        self.host_info = {}
        metrics = [
            ("CPU 使用率", "cpu_pct", "%"),
            ("内存使用率", "mem_pct", "%"),
        ]
        self.host_bars = {}
        r = 0
        for lab, key, unit in metrics:
            ttk.Label(f, text=lab + ":", anchor="e", width=16).grid(row=r, column=0, sticky="e", padx=8, pady=6)
            bar = ttk.Progressbar(f, maximum=100, length=360)
            bar.grid(row=r, column=1, sticky="w", padx=8, pady=6)
            val = ttk.Label(f, text="—", width=20, anchor="w")
            val.grid(row=r, column=2, sticky="w", padx=8)
            self.host_bars[key] = (bar, val, unit)
            r += 1

        info_rows = [
            ("CPU 型号", "cpu_model"), ("CPU 核数", "cpu_cores"),
            ("CPU 线程数", "cpu_threads"), ("CPU 总量", "cpu_total_mhz"),
            ("CPU 已用", "cpu_usage_mhz"), ("内存总量", "mem_total_mb"),
            ("内存已用", "mem_usage_mb"), ("开机时间", "boot_time"),
            ("运行时长", "uptime"), ("UUID", "uuid"),
        ]
        for lab, key in info_rows:
            ttk.Label(f, text=lab + ":", anchor="e", width=16).grid(row=r, column=0, sticky="e", padx=8, pady=3)
            v = ttk.Label(f, text="—", anchor="w")
            v.grid(row=r, column=1, columnspan=2, sticky="w", padx=8, pady=3)
            self.host_info[key] = v
            r += 1

    # ---- 虚拟机 ----
    def _build_vm_tab(self):
        f = ttk.Frame(self.notebook)
        self.notebook.add(f, text="虚拟机")

        # 顶部：查看方式切换（默认详细信息）
        bar = ttk.Frame(f)
        bar.pack(fill=tk.X, padx=6, pady=4)
        ttk.Label(bar, text="查看方式:").pack(side=tk.LEFT)
        self.vm_view_mode = tk.StringVar(value="detail")
        ttk.Radiobutton(bar, text="详细信息", variable=self.vm_view_mode,
                        value="detail", command=self._toggle_vm_view).pack(side=tk.LEFT, padx=4)
        ttk.Radiobutton(bar, text="卡片", variable=self.vm_view_mode,
                        value="card", command=self._toggle_vm_view).pack(side=tk.LEFT, padx=4)

        # 操作栏：对选中的虚拟机执行电源 / 克隆 / 控制台操作
        action_bar = ttk.Frame(f)
        action_bar.pack(fill=tk.X, padx=6, pady=2)
        ttk.Button(action_bar, text="开机", width=8,
                   command=lambda: self._run_vm_action("on")).pack(side=tk.LEFT, padx=2)
        ttk.Button(action_bar, text="关机", width=8,
                   command=lambda: self._run_vm_action("off")).pack(side=tk.LEFT, padx=2)
        ttk.Button(action_bar, text="暂停", width=8,
                   command=lambda: self._run_vm_action("suspend")).pack(side=tk.LEFT, padx=2)
        ttk.Button(action_bar, text="重启", width=8,
                   command=lambda: self._run_vm_action("reset")).pack(side=tk.LEFT, padx=2)
        ttk.Button(action_bar, text="克隆", width=8,
                   command=lambda: self._run_vm_action("clone")).pack(side=tk.LEFT, padx=2)
        ttk.Button(action_bar, text="打开控制台", width=10,
                   command=lambda: self._open_console("vmrc")).pack(side=tk.LEFT, padx=2)
        ttk.Button(action_bar, text="Web控制台", width=10,
                   command=lambda: self._open_console("web")).pack(side=tk.LEFT, padx=2)
        self.vm_sel_label = ttk.Label(action_bar, text="当前选择: 无")
        self.vm_sel_label.pack(side=tk.LEFT, padx=10)

        # 详细信息表格右键菜单
        self.vm_menu = tk.Menu(self.root, tearoff=0)
        for act, lab in (("on", "开机"), ("off", "关机"),
                         ("suspend", "暂停"), ("reset", "重启"),
                         ("clone", "克隆")):
            self.vm_menu.add_command(label=lab, command=lambda a=act: self._run_vm_action(a))
        self.vm_menu.add_separator()
        self.vm_menu.add_command(label="打开控制台", command=lambda: self._open_console("vmrc"))
        self.vm_menu.add_command(label="Web控制台", command=lambda: self._open_console("web"))

        # 详细信息（表格）框架
        self.vm_detail_frame = ttk.Frame(f)
        cols = ("名称", "电源", "Guest", "Tools", "整体状态",
                "CPU%", "内存%", "内存(MB)", "IP", "系统")
        self.vm_tree = ttk.Treeview(self.vm_detail_frame, columns=cols, show="headings", height=20)
        widths = (160, 90, 90, 130, 90, 60, 60, 90, 130, 160)
        for c, w in zip(cols, widths):
            self.vm_tree.heading(c, text=c)
            self.vm_tree.column(c, width=w)
        self.vm_tree.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)
        self.vm_tree.bind("<<TreeviewSelect>>", self._on_vm_tree_select)
        self.vm_tree.bind("<Button-3>", self._on_vm_right_click)
        self.vm_detail_frame.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

        # 卡片框架（带滚动）
        self.vm_card_frame = ttk.Frame(f)
        self.vm_card_canvas = tk.Canvas(self.vm_card_frame)
        self.vm_card_scroll = ttk.Scrollbar(self.vm_card_frame, orient=tk.VERTICAL,
                                           command=self.vm_card_canvas.yview)
        self.vm_card_inner = ttk.Frame(self.vm_card_canvas)
        self.vm_card_canvas.create_window((0, 0), window=self.vm_card_inner, anchor="nw")
        self.vm_card_canvas.configure(yscrollcommand=self.vm_card_scroll.set)
        self.vm_card_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.vm_card_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.vm_card_inner.bind(
            "<Configure>",
            lambda e: self.vm_card_canvas.configure(scrollregion=self.vm_card_canvas.bbox("all")),
        )
        self.vm_card_canvas.bind(
            "<MouseWheel>",
            lambda e: self.vm_card_canvas.yview_scroll(int(-1 * (e.delta / 120)), "units"),
        )
        # 初始隐藏卡片视图
        self.vm_card_frame.pack_forget()

    def _toggle_vm_view(self):
        if self.vm_view_mode.get() == "detail":
            self.vm_card_frame.pack_forget()
            self.vm_detail_frame.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)
        else:
            self.vm_detail_frame.pack_forget()
            self.vm_card_frame.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

    # ---- 存储 ----
    def _build_storage_tab(self):
        f = ttk.Frame(self.notebook)
        self.notebook.add(f, text="存储")
        cols = ("名称", "类型", "容量(GB)", "可用(GB)", "已用%", "可访问")
        self.ds_tree = ttk.Treeview(f, columns=cols, show="headings", height=20)
        for c, w in zip(cols, (200, 100, 110, 110, 90, 80)):
            self.ds_tree.heading(c, text=c)
            self.ds_tree.column(c, width=w)
        self.ds_tree.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

    # ---------------- 连接管理 ----------------
    def _refresh_conn_list(self):
        self.conn_list.delete(0, tk.END)
        for c in self.config.connections:
            self.conn_list.insert(tk.END, c.get("name", c.get("host", "?")))

    def new_connection(self):
        dlg = ConnectionDialog(self.root, "新建连接")
        if dlg.result and dlg.result["name"] and dlg.result["host"]:
            self.config.add(dlg.result)
            self._refresh_conn_list()

    def edit_connection(self):
        idx = self._selected_conn_idx()
        if idx is None:
            return
        dlg = ConnectionDialog(self.root, "编辑连接", self.config.connections[idx])
        if dlg.result and dlg.result["name"] and dlg.result["host"]:
            self.config.update(idx, dlg.result)
            self._refresh_conn_list()

    def delete_connection(self):
        idx = self._selected_conn_idx()
        if idx is None:
            return
        name = self.config.connections[idx].get("name", "")
        if messagebox.askyesno("确认删除", "确定删除连接「%s」？" % name):
            self.config.remove(idx)
            self._refresh_conn_list()

    def _selected_conn_idx(self):
        sel = self.conn_list.curselection()
        if not sel:
            messagebox.showinfo("提示", "请先在左侧选择一条连接。")
            return None
        return sel[0]

    def connect_selected(self):
        idx = self._selected_conn_idx()
        if idx is None:
            return
        conn = self.config.connections[idx]
        if self.client:
            self.client.disconnect()
            self.client = None
        self.current_conn = conn
        self.set_status("正在连接 %s ..." % conn.get("host", ""))
        self.connect_done = False
        # 看门狗：若长时间未连上（连接线程卡死），给出可诊断的提示
        self.root.after(25000, self._connect_watchdog)
        threading.Thread(target=self._worker_connect, args=(conn,), daemon=True).start()

    # ---------------- 后台任务 ----------------
    def _worker_connect(self, conn):
        try:
            client = VSphereClient(
                host=conn["host"],
                username=conn["username"],
                password=ConfigStore._decode(conn.get("password", "")),
                port=conn.get("port", 443),
                verify_ssl=conn.get("verify_ssl", False),
            )
            client.connect()
            self.client = client
            data = client.get_data()
            self.root.after(0, self._on_data, data, None)
        except Exception as e:
            self.root.after(0, self._on_error, "连接失败: %s" % e)

    def refresh(self):
        if not self.client:
            messagebox.showinfo("提示", "请先连接一个 ESXi / vCenter。")
            return
        self.set_status("正在刷新数据 ...")
        threading.Thread(target=self._worker_refresh, daemon=True).start()

    def _worker_refresh(self):
        try:
            data = self.client.get_data()
            self.root.after(0, self._on_data, data, None)
        except Exception as e:
            # 会话可能失效，尝试重连一次
            try:
                self.client.disconnect()
                self.client = None
                conn = self.current_conn
                client = VSphereClient(
                    host=conn["host"], username=conn["username"],
                    password=ConfigStore._decode(conn.get("password", "")),
                    port=conn.get("port", 443),
                    verify_ssl=conn.get("verify_ssl", False),
                )
                client.connect()
                self.client = client
                data = client.get_data()
                self.root.after(0, self._on_data, data, None)
            except Exception as e2:
                self.root.after(0, self._on_error, "刷新失败: %s" % e2)

    # ---------------- 数据填充 ----------------
    def _on_data(self, data, _):
        self.connect_done = True
        try:
            self.last_data = data
            hosts = data.get("hosts", [])
            # 主机下拉
            names = [h.get("name", "?") for h in hosts]
            self.host_combo["values"] = names
            if names:
                self.last_host_idx = 0
                self.host_combo.current(0)
            self.btn_refresh.config(state="normal")
            self._populate_host_tabs()
            # 虚拟机排序：已开机(poweredOn)排在最前，其余靠后；同组内按名称字母顺序
            vms = sorted(data.get("vms", []),
                         key=lambda v: (0 if v.get("power_state") == "poweredOn" else 1,
                                        (v.get("name") or "").lower()))
            self._populate_vms(vms)
            self._populate_vm_cards(vms)
            self._populate_datastores(data.get("datastores", []))
            self._populate_overview()
            self.set_status("数据已更新：%s（共 %d 台主机 / %d 台虚拟机）" % (
                data.get("collect_time", ""), len(hosts), len(data.get("vms", []))))
        except Exception as e:
            # 数据填充阶段异常也要暴露出来，避免界面卡在“正在连接”
            import traceback as _tb
            _tb.print_exc()
            self._on_error("加载/渲染数据失败: %s" % e)

    def _on_error(self, msg):
        self.connect_done = True
        self.set_status(msg)
        messagebox.showerror("错误", msg)

    def _connect_watchdog(self):
        """连接看门狗：若 25 秒内连接线程仍未给出结果（卡死），
        给出清晰的诊断提示，避免界面一直停在“正在连接”。"""
        if self.connect_done:
            return
        host = self.current_conn.get("host", "") if self.current_conn else ""
        self.set_status("连接超时：未能在限定时间内连上 %s（请检查网络/地址/端口/防火墙）" % host)
        messagebox.showwarning(
            "连接超时",
            "连接 %s 超时，连接线程可能已卡死。\n\n请逐项排查：\n"
            "• 该 ESXi 主机 IP/端口(%s) 是否正确、主机是否在线\n"
            "• 本机能否 ping 通该主机、能否访问其 443 端口（防火墙/网络策略）\n"
            "• 用户名 / 密码是否正确\n"
            "• 若使用自签名证书，连接配置里“校验 SSL”应保持关闭"
            % (host, self.current_conn.get("port", 443) if self.current_conn else 443))

    def _on_host_selected(self, _):
        idx = self.host_combo.current()
        if idx >= 0:
            self.last_host_idx = idx
            self._populate_host_tabs()

    def _host(self):
        if not self.last_data:
            return None
        hosts = self.last_data.get("hosts", [])
        if 0 <= self.last_host_idx < len(hosts):
            return hosts[self.last_host_idx]
        return None

    # ---- 主机相关标签页 ----
    def _populate_host_tabs(self):
        h = self._host()
        if not h:
            return
        # 硬件
        self.hw_overall.config(
            text=self.STATE_LABELS.get(h.get("overall_health", "unknown"), "未知"),
            foreground=self.STATE_COLORS.get(h.get("overall_health", "unknown"), "#000"),
        )
        for row in self.hw_tree.get_children():
            self.hw_tree.delete(row)
        for s in h.get("sensors", []):
            st = s.get("state", "unknown")
            self.hw_tree.insert("", tk.END, values=(
                s.get("name", ""), s.get("type", ""),
                self.STATE_LABELS.get(st, st), s.get("reading", ""),
            ))
            # 给状态列上色
            item = self.hw_tree.get_children()[-1]
            self.hw_tree.set(item, "状态", self.STATE_LABELS.get(st, st))
            tag = "st_%s" % st
            self.hw_tree.tag_configure(tag, foreground=self.STATE_COLORS.get(st, "#000"))
            self.hw_tree.item(item, tags=(tag,))

        # 主机资源
        for key, (bar, val, unit) in self.host_bars.items():
            pct = h.get(key, 0) or 0
            bar["value"] = pct
            color = "green" if pct < 70 else ("yellow" if pct < 90 else "red")
            bar.configure(style="pb_%s.Horizontal.TProgressbar" % color)
            val.config(text="%.1f %s" % (pct, unit))
        info_map = {
            "cpu_model": h.get("cpu_model", "—"),
            "cpu_cores": h.get("cpu_cores", "—"),
            "cpu_threads": h.get("cpu_threads", "—"),
            "cpu_total_mhz": "%d MHz" % (h.get("cpu_total_mhz", 0) or 0),
            "cpu_usage_mhz": "%d MHz" % (h.get("cpu_usage_mhz", 0) or 0),
            "mem_total_mb": "%.0f MB" % (h.get("mem_total_mb", 0) or 0),
            "mem_usage_mb": "%d MB" % (h.get("mem_usage_mb", 0) or 0),
            "boot_time": h.get("boot_time", "—") or "—",
            "uptime": self._fmt_uptime_pub(h.get("uptime", 0)),
            "uuid": h.get("uuid", "—") or "—",
        }
        for k, v in info_map.items():
            if k in self.host_info:
                self.host_info[k].config(text=str(v))

    # ---- 概览 ----
    def _populate_overview(self):
        h = self._host()
        vms = self.last_data.get("vms", [])
        dss = self.last_data.get("datastores", [])
        vm_on = sum(1 for v in vms if v.get("power_state") == "poweredOn")
        vals = {
            "name": h.get("name", "—") if h else "—",
            "model": ("%s %s" % (h.get("vendor", ""), h.get("model", ""))).strip() if h else "—",
            "power_state": h.get("power_state", "—") if h else "—",
            "connection_state": h.get("connection_state", "—") if h else "—",
            "uptime": self._fmt_uptime_pub(h.get("uptime", 0)) if h else "—",
            "cpu_pct": ("%.1f%%" % h.get("cpu_pct", 0)) if h else "—",
            "mem_pct": ("%.1f%%" % h.get("mem_pct", 0)) if h else "—",
            "vm_total": len(vms),
            "vm_on": vm_on,
            "ds_total": len(dss),
        }
        for k, v in vals.items():
            if k in self.ov_labels:
                self.ov_labels[k].config(text=str(v))
        if h:
            cp = h.get("cpu_pct", 0) or 0
            mp = h.get("mem_pct", 0) or 0
            self.ov_cpu_bar["value"] = cp
            self.ov_mem_bar["value"] = mp
            ccol = "green" if cp < 70 else ("yellow" if cp < 90 else "red")
            mcol = "green" if mp < 70 else ("yellow" if mp < 90 else "red")
            self.ov_cpu_bar.configure(style="pb_%s.Horizontal.TProgressbar" % ccol)
            self.ov_mem_bar.configure(style="pb_%s.Horizontal.TProgressbar" % mcol)

    # ---- 虚拟机表 ----
    def _populate_vms(self, vms):
        for row in self.vm_tree.get_children():
            self.vm_tree.delete(row)
        self.vm_tree_moids = {}
        for v in vms:
            item = self.vm_tree.insert("", tk.END, values=(
                v.get("name", "?"),
                v.get("power_state", ""),
                v.get("guest_state", ""),
                v.get("tools_status", ""),
                self.STATE_LABELS.get(v.get("overall_status", ""), v.get("overall_status", "")),
                v.get("cpu_pct", 0),
                v.get("mem_pct", 0),
                v.get("guest_mem_mb", 0),
                v.get("ip", ""),
                v.get("os", ""),
            ))
            item = self.vm_tree.get_children()[-1]
            st = v.get("overall_status", "")
            tag = "st_%s" % st
            self.vm_tree.tag_configure(tag, foreground=self.STATE_COLORS.get(st, "#000"))
            self.vm_tree.item(item, tags=(tag,))
            self.vm_tree_moids[item] = v.get("moid", "")

    # ---- 虚拟机卡片 ----
    # 选中高亮使用 tk 级 highlight 边框（与 ttk 主题样式解耦，避免样式布局缺失报错）
    CARD_BORDER_DEFAULT = "#bdbdbd"
    CARD_BORDER_SELECTED = "#c62828"

    def _populate_vm_cards(self, vms):
        # 清空旧卡片
        for w in self.vm_card_inner.winfo_children():
            w.destroy()

        pw_map = {"poweredOn": ("已开机", "#2e7d32"),
                  "poweredOff": ("已关机", "#757575"),
                  "suspended": ("已挂起", "#f9a825")}
        cols = 2
        self.selected_card = None  # 重建后重置，下面按需重新高亮
        for i, v in enumerate(vms):
            name = v.get("name", "?")
            moid = v.get("moid", "")
            is_selected = (moid == self.selected_vm_moid)
            # 用 tk.Frame + 手动标题：tk.Frame 支持 highlight 边框，跨主题稳定
            card = tk.Frame(self.vm_card_inner, bg="#ffffff", bd=1, relief="solid")
            card._orig_text = name
            card.grid(row=i // cols, column=i % cols, padx=8, pady=8, sticky="nsew")
            if is_selected:
                card.configure(highlightbackground=self.CARD_BORDER_SELECTED,
                               highlightcolor=self.CARD_BORDER_SELECTED,
                               highlightthickness=2)
                self.selected_card = card
            else:
                card.configure(highlightbackground=self.CARD_BORDER_DEFAULT,
                               highlightcolor=self.CARD_BORDER_DEFAULT,
                               highlightthickness=1)
            # 手动标题（带 ✓ 表示选中）
            title = ttk.Label(
                card,
                text=("✓ " + name) if is_selected else name,
                font=("Microsoft YaHei", 10, "bold"),
                foreground=self.CARD_BORDER_SELECTED if is_selected else "#37474f")
            title.grid(row=0, column=0, columnspan=2, sticky="w", padx=8, pady=(6, 6))
            card._title = title
            card.bind("<Button-1>", lambda e, m=moid, n=name, c=card: self._select_vm(m, n, c))

            pw, pw_color = pw_map.get(v.get("power_state", ""), (v.get("power_state", ""), "#000"))
            self._card_row(card, 1, "电源状态", pw, pw_color)
            self._card_row(card, 2, "Tools", v.get("tools_status", ""), "#000")
            st = v.get("overall_status", "")
            self._card_row(card, 3, "整体状态",
                           self.STATE_LABELS.get(st, st),
                           self.STATE_COLORS.get(st, "#000"))

            cpu_pct = v.get("cpu_pct", 0) or 0
            mem_pct = v.get("mem_pct", 0) or 0
            self._card_bar(card, 4, "CPU", cpu_pct)
            self._card_bar(card, 5, "内存", mem_pct)

            ttk.Label(card, text="IP: %s" % (v.get("ip", "") or "—")).grid(
                row=6, column=0, columnspan=2, sticky="w", padx=8, pady=2)
            ttk.Label(card, text="系统: %s" % (v.get("os", "") or "—")).grid(
                row=7, column=0, columnspan=2, sticky="w", padx=8, pady=2)
            ttk.Label(card, text="配置: %d vCPU / %d MB" % (
                v.get("num_cpu", 0), v.get("mem_size_mb", 0))).grid(
                row=8, column=0, columnspan=2, sticky="w", padx=8, pady=2)

        for c in range(cols):
            self.vm_card_inner.columnconfigure(c, weight=1)

    # ---- 虚拟机操作（UI 层）----
    def _select_vm(self, moid, name, card=None):
        self.selected_vm_moid = moid
        self.selected_vm_name = name
        self.vm_sel_label.config(text="当前选择: %s" % name)
        # 卡片高亮（tk highlight 边框 + 标题 ✓ / 颜色）：撤销上一个，高亮当前
        if self.selected_card is not None and self.selected_card is not card:
            self.selected_card.configure(highlightbackground=self.CARD_BORDER_DEFAULT,
                                         highlightcolor=self.CARD_BORDER_DEFAULT,
                                         highlightthickness=1)
            if getattr(self.selected_card, "_title", None):
                self.selected_card._title.configure(
                    text=getattr(self.selected_card, "_orig_text", ""),
                    foreground="#37474f")
        if card is not None:
            card.configure(highlightbackground=self.CARD_BORDER_SELECTED,
                           highlightcolor=self.CARD_BORDER_SELECTED,
                           highlightthickness=2)
            if getattr(card, "_title", None):
                card._title.configure(text="✓ " + getattr(card, "_orig_text", name),
                                      foreground=self.CARD_BORDER_SELECTED)
            self.selected_card = card
        else:
            self.selected_card = None

    def _on_vm_tree_select(self, _):
        sel = self.vm_tree.selection()
        if sel:
            moid = self.vm_tree_moids.get(sel[0], "")
            vals = self.vm_tree.item(sel[0], "values")
            name = vals[0] if vals else ""
            # 从表格选择时，撤销卡片高亮
            if self.selected_card is not None:
                self.selected_card.configure(highlightbackground=self.CARD_BORDER_DEFAULT,
                                             highlightcolor=self.CARD_BORDER_DEFAULT,
                                             highlightthickness=1)
                if getattr(self.selected_card, "_title", None):
                    self.selected_card._title.configure(
                        text=getattr(self.selected_card, "_orig_text", ""),
                        foreground="#37474f")
                self.selected_card = None
            self._select_vm(moid, name)

    def _on_vm_right_click(self, event):
        item = self.vm_tree.identify_row(event.y)
        if item:
            self.vm_tree.selection_set(item)
            self._on_vm_tree_select(None)
            self.vm_menu.post(event.x_root, event.y_root)

    def _refresh_async(self):
        if not self.client:
            return
        threading.Thread(target=self._worker_refresh, daemon=True).start()

    def _run_vm_action(self, action):
        if not self.client:
            messagebox.showwarning("未连接", "请先连接 ESXi / vCenter。")
            return
        if not self.selected_vm_moid:
            messagebox.showinfo("提示", "请先在列表或卡片中选择一台虚拟机。")
            return
        # 电源类操作（开机/关机/暂停/重启）需要二次确认
        if action in ("on", "off", "suspend", "reset"):
            ok = messagebox.askyesno(
                "确认操作",
                "确定要对虚拟机「%s」执行【%s】操作吗？\n\n该操作会立即影响虚拟机的运行状态。"
                % (self.selected_vm_name, self._action_label(action)))
            if not ok:
                return
        new_name = None
        if action == "clone":
            new_name = simpledialog.askstring(
                "克隆虚拟机", "请输入新虚拟机的名称：",
                initialvalue="%s-副本" % (self.selected_vm_name or "vm"))
            if not new_name:
                return
        self.set_status("正在对「%s」执行%s ..." % (
            self.selected_vm_name, self._action_label(action)))
        threading.Thread(target=self._do_vm_action,
                         args=(action, self.selected_vm_moid, new_name),
                         daemon=True).start()

    @staticmethod
    def _action_label(action):
        return {"on": "开机", "off": "关机", "suspend": "暂停",
                "reset": "重启", "clone": "克隆"}.get(action, action)

    def _do_vm_action(self, action, moid, new_name):
        try:
            ok, msg = self.client.vm_action(action, moid, new_name)
        except Exception as e:
            ok, msg = False, "操作异常: %s" % e
        self.root.after(0, self._on_vm_action_done, ok, msg)

    def _on_vm_action_done(self, ok, msg):
        if ok:
            self.set_status("虚拟机操作成功：%s" % msg)
        else:
            messagebox.showerror("操作失败", msg)
            self.set_status("虚拟机操作失败：%s" % msg)
        # 操作后刷新（电源状态会变化）
        self._refresh_async()

    def _open_console(self, kind):
        if not self.client:
            messagebox.showwarning("未连接", "请先连接 ESXi / vCenter。")
            return
        if not self.selected_vm_moid:
            messagebox.showinfo("提示", "请先选择一台虚拟机。")
            return

        def _work():
            try:
                info = self.client.console_tickets(self.selected_vm_moid)
            except Exception as e:
                self.root.after(0, lambda: messagebox.showerror(
                    "控制台错误", "获取控制台失败: %s" % e))
                return
            if not info:
                self.root.after(0, lambda: messagebox.showerror(
                    "控制台错误", "未找到该虚拟机，请刷新后重试。"))
                return
            if kind == "vmrc":
                url = info.get("vmrc_url")
                if not url:
                    # VMRC 票据获取失败（多为虚拟机关机 / 本机未安装 VMRC），
                    # 自动改用浏览器 Web 控制台打开（关机状态也能连）
                    import webbrowser
                    web = info.get("web_url", "")
                    webbrowser.open(web)
                    self.root.after(0, lambda: messagebox.showinfo(
                        "已改用 Web 控制台",
                        "VMRC 无法打开：%s\n\n已自动改用浏览器打开 Web 控制台"
                        "（该方式适用于关机状态的虚拟机）。" % (info.get("vmrc_error", "") or "未知原因")))
                    return
                try:
                    os.startfile(url)
                    self.root.after(0, lambda: self.set_status(
                        "已尝试用 VMware Remote Console 打开：%s" % self.selected_vm_name))
                except Exception as e:
                    # 未安装 VMRC 时回退到浏览器 Web 控制台
                    try:
                        import webbrowser
                        webbrowser.open(info.get("web_url", ""))
                        self.root.after(0, lambda: self.set_status(
                            "未找到 VMRC，已改在浏览器打开 Web 控制台。"))
                    except Exception as e2:
                        self.root.after(0, lambda: messagebox.showerror(
                            "打开失败", "VMRC 启动失败：%s\n浏览器打开也失败：%s" % (e, e2)))
            else:
                import webbrowser
                webbrowser.open(info.get("web_url", ""))
                self.root.after(0, lambda: self.set_status(
                    "已在浏览器打开 Web 控制台：%s" % self.selected_vm_name))

        threading.Thread(target=_work, daemon=True).start()

    def _card_row(self, parent, row, label, value, color):
        ttk.Label(parent, text=label + ":", width=10, anchor="e").grid(
            row=row, column=0, sticky="e", padx=2, pady=2)
        ttk.Label(parent, text=value, foreground=color, width=14, anchor="w").grid(
            row=row, column=1, sticky="w", padx=2, pady=2)

    def _card_bar(self, parent, row, label, pct):
        ttk.Label(parent, text=label + ":", width=10, anchor="e").grid(
            row=row, column=0, sticky="e", padx=2, pady=2)
        bar = ttk.Progressbar(parent, maximum=100, length=130)
        bar.grid(row=row, column=1, sticky="w", padx=2, pady=2)
        bar["value"] = pct
        color = "green" if pct < 70 else ("yellow" if pct < 90 else "red")
        bar.configure(style="pb_%s.Horizontal.TProgressbar" % color)
        ttk.Label(parent, text="%.1f%%" % pct, width=8, anchor="w").grid(
            row=row, column=2, sticky="w", padx=2, pady=2)

    # ---- 存储表 ----
    def _populate_datastores(self, dss):
        for row in self.ds_tree.get_children():
            self.ds_tree.delete(row)
        for d in dss:
            self.ds_tree.insert("", tk.END, values=(
                d.get("name", "?"),
                d.get("type", ""),
                d.get("capacity_gb", 0),
                d.get("free_gb", 0),
                d.get("used_pct", 0),
                "是" if d.get("accessible") else "否",
            ))
            item = self.ds_tree.get_children()[-1]
            pct = d.get("used_pct", 0) or 0
            tag = "st_green" if pct < 70 else ("st_yellow" if pct < 90 else "st_red")
            self.ds_tree.tag_configure("st_red", foreground="#c62828")
            self.ds_tree.tag_configure("st_yellow", foreground="#f9a825")
            self.ds_tree.tag_configure("st_green", foreground="#2e7d32")
            self.ds_tree.item(item, tags=(tag,))

    # ---------------- 自动刷新 ----------------
    def _toggle_auto(self):
        self.auto_refresh = self.var_auto.get()
        if self.auto_refresh:
            self._schedule_auto()
        else:
            if self.auto_timer:
                self.root.after_cancel(self.auto_timer)
                self.auto_timer = None

    def _schedule_auto(self):
        if not self.auto_refresh:
            return
        try:
            interval = int(self.e_interval.get() or 30) * 1000
        except Exception:
            interval = 30000
        self.auto_timer = self.root.after(interval, self._auto_tick)

    def _auto_tick(self):
        if not self.auto_refresh:
            return
        if self.client:
            threading.Thread(target=self._worker_refresh, daemon=True).start()
        self._schedule_auto()

    # ---------------- 杂项 ----------------
    def set_status(self, msg):
        self.status_var.set(msg)

    @staticmethod
    def _fmt_uptime_pub(seconds):
        return VSphereClient._fmt_uptime(seconds)

    def _on_close(self):
        if self.client:
            try:
                self.client.disconnect()
            except Exception:
                pass
        self.root.destroy()


# ==============================================================
# 启动引导：双击运行时的解释器自愈与无控制台处理
# ==============================================================
def _init_logging():
    """无控制台（pythonw）运行时 stdout/stderr 为 None，
    重定向到日志文件，避免 print 崩溃，也便于排查静默退出。"""
    try:
        log_file = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "esxi_monitor.log")
        if sys.stdout is None or not hasattr(sys.stdout, "write"):
            sys.stdout = open(log_file, "a", encoding="utf-8")
        if sys.stderr is None or not hasattr(sys.stderr, "write"):
            sys.stderr = open(log_file, "a", encoding="utf-8")
    except Exception:
        pass


def _relaunch_via_pythonw():
    """若当前是控制台 python.exe，则改用同目录 / PATH 中的 pythonw.exe 重启，
    从而彻底隐藏黑色命令行窗口。"""
    exe = sys.executable or ""
    if os.path.basename(exe).lower() == "pythonw.exe":
        return False
    sib = os.path.join(os.path.dirname(os.path.abspath(exe)), "pythonw.exe")
    interp = sib if os.path.isfile(sib) else None
    if not interp:
        try:
            import shutil
            interp = shutil.which("pythonw")
        except Exception:
            interp = None
    if interp and os.path.abspath(interp) != os.path.abspath(exe):
        try:
            import subprocess
            subprocess.Popen([interp, os.path.abspath(__file__)])
            return True
        except Exception:
            return False
    return False


def _find_interpreter_with_pyvmomi():
    """查找本机装了 pyVmomi 的 Python 解释器，优先返回 pythonw（无控制台）。"""
    import subprocess
    cur = sys.executable or ""
    candidates = []
    if cur:
        d = os.path.dirname(os.path.abspath(cur))
        candidates.append(os.path.join(d, "pythonw.exe"))
        candidates.append(os.path.join(d, "python.exe"))
    local = os.environ.get("LOCALAPPDATA", "")
    for ver in ("Python313", "Python312", "Python311", "Python310",
                "Python39", "Python38"):
        base = os.path.join(local, ver) if local else ""
        if base:
            candidates.append(os.path.join(base, "pythonw.exe"))
            candidates.append(os.path.join(base, "python.exe"))
    if local:
        candidates.append(os.path.join(local, "Python", "pythonw.exe"))
    try:
        import shutil
        for name in ("pythonw", "python", "py"):
            p = shutil.which(name)
            if p:
                candidates.append(p)
    except Exception:
        pass

    seen = set()
    # 第一轮：只接受 pythonw（无控制台）
    for c in candidates:
        if not c or c in seen:
            continue
        seen.add(c)
        if not os.path.isfile(c):
            continue
        if os.path.abspath(c) == os.path.abspath(cur):
            continue
        try:
            r = subprocess.run([c, "-c", "import pyVmomi"],
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=20)
            if r.returncode == 0 and c.lower().endswith("pythonw.exe"):
                return c
        except Exception:
            continue
    # 第二轮：接受任意解释器（兜底）
    for c in candidates:
        if not c or not os.path.isfile(c):
            continue
        if os.path.abspath(c) == os.path.abspath(cur):
            continue
        try:
            r = subprocess.run([c, "-c", "import pyVmomi"],
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=20)
            if r.returncode == 0:
                return c
        except Exception:
            continue
    return None


def main():
    root = tk.Tk()
    # 适配中文字体：字体名含空格需用花括号包裹，否则 Tk 解析报错
    try:
        import tkinter.font as tkfont
        default_font = tkfont.nametofont("TkDefaultFont")
        default_font.configure(family="Microsoft YaHei", size=10)
        # ttk 控件读取 *Font 资源，需用 "{Microsoft YaHei} 10" 规范写法
        root.option_add("*Font", "{Microsoft YaHei} 10")
    except Exception:
        pass
    ESXiMonitorApp(root)
    root.mainloop()


if __name__ == "__main__":
    _init_logging()
    # 双击 .py 时通常由控制台 python 启动 -> 改用 pythonw 消除黑框
    if _relaunch_via_pythonw():
        sys.exit(0)
    # 若当前解释器缺 pyVmomi，尝试用装了 pyVmomi 的解释器（优先 pythonw）重启
    if not HAS_PYVMOMI:
        interp = _find_interpreter_with_pyvmomi()
        if interp:
            import subprocess
            try:
                subprocess.Popen([interp, os.path.abspath(__file__)])
            except Exception:
                pass
            sys.exit(0)
    try:
        main()
    except Exception:
        import traceback as _tb
        err = _tb.format_exc()
        try:
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "esxi_monitor_error.log"), "a", encoding="utf-8") as _f:
                _f.write(err + "\n")
        except Exception:
            pass
        try:
            import tkinter as _tk
            from tkinter import messagebox as _mb
            _root = _tk.Tk()
            _root.withdraw()
            _mb.showerror("程序异常退出",
                          "程序发生未捕获错误，已记录到 esxi_monitor_error.log：\n\n" + err)
        except Exception:
            pass
