#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""串口功能自检脚本（配合 hw3 工程使用）

用法：
    python tools/serial_test.py                 # 自动找 CH340/CP210x 端口
    python tools/serial_test.py -p COM3         # 指定端口
    python tools/serial_test.py --manual 1 0 300   # 手动发一包：RUN DIR STEP

做的事：
    1. 以 115200-8-N-1 打开串口，先读掉上电/复位时的那行反馈；
    2. 依次发 4 组标准数据包，校验收到的反馈是否与预期一致；
    3. 发 3 组"坏包"（错校验和、错帧头、间隔越界），确认它们被丢弃、状态不变；
    4. 打印一张 OK/FAIL 表格，全过则退出码为 0。

注意：运行前先关掉串口助手，同一个 COM 口不能被两个程序同时打开。
"""

import argparse
import sys
import time

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    sys.exit("缺少 pyserial，先执行：python -m pip install pyserial")

BAUD = 115200


def build_packet(run, direction, step_ms):
    """按作业格式拼包：AA RUN DIR STEP_L STEP_H CHECK"""
    body = [0xAA, run & 1, direction & 1, step_ms & 0xFF, (step_ms >> 8) & 0xFF]
    return bytes(body + [sum(body) & 0xFF])


def hexstr(data):
    return " ".join("%02X" % b for b in data)


def find_port(explicit=None):
    if explicit:
        return explicit
    ports = list(list_ports.comports())
    if not ports:
        return None
    prefs = ("CH340", "CH341", "CP210", "USB-SERIAL", "USB Serial")
    for p in ports:
        text = "%s %s" % (p.description or "", p.hwid or "")
        if any(k.lower() in text.lower() for k in prefs):
            return p.device
    return ports[0].device


def read_line(ser, timeout=1.5):
    """读到 \\n 为止；超时返回已经收到的内容"""
    deadline = time.time() + timeout
    buf = bytearray()
    while time.time() < deadline:
        chunk = ser.read(1)
        if chunk:
            buf += chunk
            if chunk == b"\n":
                break
    return buf.decode("ascii", "replace").strip()


def drain(ser, seconds=0.3):
    deadline = time.time() + seconds
    while time.time() < deadline:
        ser.read(64)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-p", "--port", help="串口号，如 COM3")
    ap.add_argument("--manual", nargs=3, metavar=("RUN", "DIR", "STEP"),
                    help="只发一包并打印反馈")
    args = ap.parse_args()

    port = find_port(args.port)
    if not port:
        sys.exit("没有找到可用串口：检查 USB-TTL 是否插好、驱动是否装上（设备管理器看 COM 口）")

    print("端口：%s  波特率：%d-8-N-1" % (port, BAUD))
    try:
        ser = serial.Serial(port, BAUD, timeout=0.2)
    except serial.SerialException as exc:
        sys.exit("打开 %s 失败：%s\n（串口助手是不是还开着？）" % (port, exc))

    ok_cnt = 0
    total = 0

    with ser:
        time.sleep(0.2)
        drain(ser, 0.3)
        ser.reset_input_buffer()

        if args.manual:
            run, direction, step = (int(x) for x in args.manual)
            pkt = build_packet(run, direction, step)
            ser.write(pkt)
            print("发送：%s" % hexstr(pkt))
            print("收到：%s" % (read_line(ser) or "(无响应)"))
            return

        # ---- 1. 复位后应主动上报一次当前状态 ----
        print("\n[1] 上电/复位反馈")
        ser.write(bytes([0xAA, 0x01, 0x00, 0x2C, 0x01, 0xD8]))  # 先发一包触发一次反馈
        line = read_line(ser)
        total += 1
        if line.startswith("STATE="):
            ok_cnt += 1
            print("  OK   %s" % line)
        else:
            print("  FAIL 没收到 STATE= 开头的文本，实际收到：%r" % line)
            print("       先确认：PA9->模块RX、PA10<-模块TX、共地、串口助手已关闭")

        # ---- 2. 四组标准包 ----
        cases = [
            ("运行 正向 300ms", 1, 0, 300, "STATE=RUN,DIR=FWD,STEP=300"),
            ("暂停 正向 300ms", 0, 0, 300, "STATE=PAUSE,DIR=FWD,STEP=300"),
            ("运行 反向 300ms", 1, 1, 300, "STATE=RUN,DIR=REV,STEP=300"),
            ("暂停 反向 1000ms", 0, 1, 1000, "STATE=PAUSE,DIR=REV,STEP=1000"),
            ("运行 正向 100ms（下边界）", 1, 0, 100, "STATE=RUN,DIR=FWD,STEP=100"),
            ("运行 正向 2000ms（上边界）", 1, 0, 2000, "STATE=RUN,DIR=FWD,STEP=2000"),
        ]
        print("\n[2] 标准数据包")
        for name, run, direction, step, expect in cases:
            pkt = build_packet(run, direction, step)
            drain(ser, 0.1)
            ser.write(pkt)
            line = read_line(ser)
            total += 1
            if line.startswith(expect):
                ok_cnt += 1
                print("  OK   %-26s %s  ->  %s" % (name, hexstr(pkt), line))
            else:
                print("  FAIL %-26s %s  ->  %r（期望以 %s 开头）" % (name, hexstr(pkt), line, expect))

        # ---- 3. 坏包必须被丢弃 ----
        print("\n[3] 非法包应被丢弃（正确表现是“无响应”）")
        bad_cases = [
            ("校验和错", bytes([0xAA, 0x01, 0x00, 0x2C, 0x01, 0x00])),
            ("帧头错", bytes([0xAB, 0x01, 0x00, 0x2C, 0x01, 0xD9])),
            ("间隔 50ms 越界", build_packet(1, 0, 50)),
        ]
        for name, pkt in bad_cases:
            drain(ser, 0.1)
            ser.write(pkt)
            line = read_line(ser, 0.8)
            total += 1
            if not line:
                ok_cnt += 1
                print("  OK   %-16s %s  ->  无响应（已丢弃）" % (name, hexstr(pkt)))
            else:
                print("  FAIL %-16s %s  ->  %r（不该有反馈）" % (name, hexstr(pkt), line))

        # ---- 4. 坏包之后状态应保持最后一包设置 ----
        print("\n[4] 丢弃后状态应保持（仍为 运行/正向/2000ms）")
        drain(ser, 0.1)
        ser.write(build_packet(1, 0, 2000))
        line = read_line(ser)
        total += 1
        if line.startswith("STATE=RUN,DIR=FWD,STEP=2000"):
            ok_cnt += 1
            print("  OK   %s" % line)
        else:
            print("  FAIL %r" % line)

    print("\n结果：%d/%d 通过" % (ok_cnt, total))
    sys.exit(0 if ok_cnt == total else 1)


if __name__ == "__main__":
    main()
