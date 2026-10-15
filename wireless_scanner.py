#!/usr/bin/env python3
"""Termux wireless scanner and network toolkit.

Modern TUI version with extra features: Wi-Fi scan, test latency/download/upload,
website checks, scheduling SMS/calls, history and config persistence.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import shutil
import socket
import ssl
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse

try:
    from rich.align import Align
    from rich.columns import Columns
    from rich.console import Console, Group
    from rich.live import Live
    from rich.panel import Panel
    from rich.prompt import Confirm, Prompt
    from rich.table import Table
    from rich.text import Text
    from rich import box
    RICH_AVAILABLE = True
except Exception:
    RICH_AVAILABLE = False

# ----------------------------
# Utility and terminal helpers
# ----------------------------

R = "\033[0m"
B = "\033[1m"
D = "\033[2m"


def c(n: int) -> str:
    return f"\033[38;5;{n}m"


CY, GR, YE, RE, GY = c(45), c(82), c(220), c(203), c(245)


def width() -> int:
    try:
        return max(36, min(shutil.get_terminal_size((46, 24)).columns, 80))
    except Exception:
        return 60


def clear() -> None:
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.flush()


if RICH_AVAILABLE:
    console = Console()
else:
    console = None


def print_line(text: str = "", style: str = "") -> None:
    if console is not None:
        console.print(Text(str(text), style=style))
    else:
        if style:
            print(f"{style}{text}{R}")
        else:
            print(text)


def notify(title: str, message: str) -> None:
    for cmd in (["termux-toast", title, message], ["termux-notification", "-t", title, message]):
        path = shutil.which(cmd[0])
        if path:
            try:
                subprocess.run(cmd, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                break
            except Exception:
                pass


# ----------------------------
# Config and history storage
# ----------------------------

CONFIG_DIR = Path.home() / ".config" / "wireless_scanner"
CONFIG_FILE = CONFIG_DIR / "config.json"
HISTORY_DB = CONFIG_DIR / "history.db"


def ensure_dirs() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def load_config() -> dict:
    ensure_dirs()
    if not CONFIG_FILE.exists():
        cfg = {
            "theme": "default",
            "show_advanced": True,
            "auto_notify": True,
            "download_url": "https://speed.cloudflare.com/__down?bytes=10000000",
            "network_timeout": 10,
            "history_limit": 50,
        }
        save_config(cfg)
        return cfg
    try:
        with CONFIG_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return load_config()
        return data
    except Exception:
        return {
            "theme": "default",
            "show_advanced": True,
            "auto_notify": True,
            "download_url": "https://speed.cloudflare.com/__down?bytes=10000000",
            "network_timeout": 10,
            "history_limit": 50,
        }


def save_config(cfg: dict) -> None:
    ensure_dirs()
    with CONFIG_FILE.open("w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def init_history_db() -> None:
    ensure_dirs()
    conn = sqlite3.connect(HISTORY_DB)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            label TEXT,
            detail TEXT,
            created_at TEXT NOT NULL,
            payload TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def append_history(kind: str, label: str, detail: str, payload: dict | None = None) -> None:
    try:
        init_history_db()
        payload_json = json.dumps(payload or {}, ensure_ascii=False)
        conn = sqlite3.connect(HISTORY_DB)
        conn.execute(
            "INSERT INTO history(kind, label, detail, created_at, payload) VALUES (?, ?, ?, ?, ?)",
            (kind, label, detail, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), payload_json),
        )
        limit = int(load_config().get("history_limit", 50))
        conn.execute(
            "DELETE FROM history WHERE id NOT IN (SELECT id FROM history ORDER BY id DESC LIMIT ?)",
            (limit,),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def get_history(limit: int = 8) -> list[dict]:
    try:
        conn = sqlite3.connect(HISTORY_DB)
        rows = conn.execute(
            "SELECT kind, label, detail, created_at, payload FROM history ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        conn.close()
        out = []
        for kind, label, detail, created_at, payload in rows:
            try:
                payload_obj = json.loads(payload or "{}")
            except Exception:
                payload_obj = {}
            out.append({
                "kind": kind,
                "label": label,
                "detail": detail,
                "created_at": created_at,
                "payload": payload_obj,
            })
        return out
    except Exception:
        return []


def run(cmd, timeout=15):
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except FileNotFoundError:
        return 127, "", f"Không tìm thấy lệnh: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", "Quét quá lâu (timeout)."
    except Exception as exc:
        return 1, "", str(exc)


def get_cmd(name):
    return shutil.which(name)


def is_termux() -> bool:
    return bool(get_cmd("termux-info") or os.path.exists("/data/data/com.termux"))


# ----------------------------
# Rich TUI rendering helpers
# ----------------------------


def make_title_panel(title: str, subtitle: str = "") -> Panel:
    text = f"[bold cyan]{title}[/bold cyan]"
    if subtitle:
        text = f"{text}\n[dim]{subtitle}[/dim]"
    return Panel(Text.from_markup(text), border_style="cyan", title="Termux Toolkit")


def render_status_table(rows: list[tuple[str, str, str]]) -> Table:
    table = Table(show_header=True, header_style="bold cyan", box=box.MINIMAL_DOUBLE_HEAD)
    table.add_column("Mục", style="bold white", min_width=18)
    table.add_column("Trạng thái", min_width=14)
    table.add_column("Ghi chú", style="dim")
    for key, value, note in rows:
        table.add_row(key, value, note)
    return table


def render_menu() -> Panel:
    table = Table.grid(expand=True)
    options = [
        ("1", "Quét Wi‑Fi"),
        ("2", "Test mạng đang dùng"),
        ("3", "So sánh Wi‑Fi / Dữ liệu"),
        ("4", "Tra website & đo tốc độ"),
        ("5", "Hẹn giờ gọi / SMS"),
        ("6", "Lịch sử & cấu hình"),
        ("7", "Kiểm tra công cụ"),
        ("8", "Hướng dẫn cài đặt"),
        ("9", "Thoát"),
    ]
    for idx, label in options:
        table.add_row(f"[bold green]{idx}[/bold green]", f"[cyan]{label}[/cyan]")
    return Panel(table, title="[bold white]MENU[/bold white]", border_style="cyan")


def render_splash() -> Panel:
    content = Group(
        Text("▂ ▄ ▆ █", style="bold green"),
        Text("NETWORK TOOLKIT", style="bold cyan"),
        Text("Wi‑Fi · Mạng · Gọi hẹn giờ · Website", style="bold white"),
        Text("Chỉ dùng cho mạng bạn có quyền kiểm tra.", style="dim"),
    )
    return Panel(content, title="[bold cyan]Termux Wireless Scanner[/bold cyan]", border_style="green")


# ----------------------------
# Termux capability checks
# ----------------------------

REQUIRED_CMDS = [
    "termux-wifi-scaninfo",
    "termux-wifi-connectioninfo",
    "termux-telephony-deviceinfo",
    "termux-telephony-call",
    "termux-sms-send",
]


def probe(name: str, timeout: int = 20) -> str:
    cmd = get_cmd(name)
    if not cmd:
        return "missing"
    rc, out, _ = run([cmd], timeout=timeout)
    if rc == 124:
        return "timeout"
    try:
        data = json.loads(out)
    except Exception:
        data = None
    if rc != 0 or (isinstance(data, dict) and data.get("API_ERROR")):
        return "perm"
    return "ok"


def check_termux_state() -> dict:
    missing = [c for c in REQUIRED_CMDS if not get_cmd(c)]
    app_ok = True if not missing else False
    if not missing:
        app_ok = probe("termux-wifi-connectioninfo", 8) != "timeout"
    perm_ok = False
    if app_ok:
        perm_ok = probe("termux-telephony-deviceinfo", 8) == "ok"
    return {
        "missing": missing,
        "app": app_ok,
        "perm": perm_ok,
    }


def offer_install() -> bool:
    print_line("\n[bold yellow]Cần cài gói termux-api.[/bold yellow]")
    print_line("  [green]pkg update -y[/green]")
    print_line("  [green]pkg install -y termux-api[/green]")
    if not get_cmd("pkg"):
        print_line("[dim]Không tìm thấy pkg; có vẻ bạn không đang ở Termux.[/dim]")
        return False
    answer = input("\nChạy luôn bây giờ? [Enter=có, n=bỏ qua]: ").strip().lower()
    if answer == "n":
        return False
    for cmd in (["pkg", "update", "-y"], ["pkg", "install", "-y", "termux-api"]):
        print_line(f"[cyan]$ {' '.join(cmd)}[/cyan]")
        if subprocess.call(cmd) != 0:
            print_line("[bold red]Lệnh lỗi. Hãy kiểm tra mạng và thử lại.[/bold red]")
            return False
    print_line("[bold green]Đã cài xong termux-api.[/bold green]")
    return True


# ----------------------------
# Wi‑Fi scanning and signal helpers
# ----------------------------


def to_int(value):
    try:
        return int(value)
    except Exception:
        return None


def scan_wifi():
    cmd = get_cmd("termux-wifi-scaninfo")
    if not cmd:
        return [], "Chưa có lệnh termux-wifi-scaninfo."

    rc, out, err = run([cmd])
    if rc != 0:
        return [], err or "Không thể quét Wi‑Fi."

    try:
        data = json.loads(out)
    except Exception:
        data = None
    if isinstance(data, dict) and data.get("API_ERROR"):
        return [], str(data["API_ERROR"])
    if not isinstance(data, list):
        return [], "Termux:API trả về dữ liệu Wi‑Fi không hợp lệ."

    rows = []
    for item in data:
        if not isinstance(item, dict):
            continue
        rows.append({
            "ssid": str(item.get("ssid") or "<Ẩn SSID>"),
            "bssid": str(item.get("bssid") or "-"),
            "freq": to_int(item.get("frequency_mhz") or item.get("frequency")),
            "rssi": to_int(item.get("rssi")),
            "cap": str(item.get("capabilities") or ""),
        })
    rows.sort(key=lambda r: r["rssi"] if r["rssi"] is not None else -999, reverse=True)
    return rows, None


def signal_bars(rssi):
    if rssi is None:
        lvl, col = 0, "[cyan]"
    elif rssi >= -55:
        lvl, col = 4, "[green]"
    elif rssi >= -65:
        lvl, col = 3, "[green]"
    elif rssi >= -75:
        lvl, col = 2, "[yellow]"
    elif rssi >= -85:
        lvl, col = 1, "[red]"
    else:
        lvl, col = 0, "[red]"
    return col + ("▂▄▆█"[:lvl] if lvl else "") + "[dim]" + ("▂▄▆█"[lvl:]) + "[/dim]"


def band(freq):
    if freq is None:
        return "?"
    if 2400 <= freq <= 2500:
        return "2.4GHz ch" + str(14 if freq == 2484 else (freq - 2407) // 5)
    if 4900 <= freq < 5925:
        return f"5GHz ch{(freq - 5000) // 5}"
    if 5925 <= freq <= 7125:
        return f"6GHz ch{(freq - 5950) // 5}"
    return f"{freq}MHz"


def security(cap):
    u = cap.upper()
    if not u:
        return "?", "[cyan]"
    if "SAE" in u or "WPA3" in u:
        return "WPA3", "[green]"
    if "WPA2" in u or "RSN" in u:
        return "WPA2", "[green]"
    if "WPA" in u:
        return "WPA", "[yellow]"
    if "WEP" in u:
        return "WEP", "[red]"
    return "Mở", "[red]"


def display_wifi(rows):
    w = width()
    if not rows:
        print_line("[yellow]Không phát hiện mạng Wi‑Fi.[/yellow]")
        return
    table = Table(show_header=False, box=None)
    table.add_column("#", style="cyan", min_width=3)
    table.add_column("Signal", min_width=6)
    table.add_column("SSID", style="bold white", overflow="fold")
    table.add_column("RSSI", style="bold green")
    table.add_column("Security", min_width=10)
    table.add_column("Band", style="dim")
    table.add_column("BSSID", style="dim")
    for i, row in enumerate(rows[:12], 1):
        bars = signal_bars(row["rssi"])
        sec, sec_style = security(row["cap"])
        bssid = row["bssid"] if w >= 48 else "…" + row["bssid"][-8:]
        rssi_text = f"{row['rssi']} dBm" if row["rssi"] is not None else "- dBm"
        table.add_row(str(i), bars, row["ssid"][:24], rssi_text, f"{sec_style}{sec}[/]", band(row["freq"]), bssid)
    print_line(Panel(table, title="[bold cyan]MẠNG WI‑FI XUNG QUANH[/bold cyan]", border_style="cyan"))
    best = rows[0]
    print_line(f"[bold green]Tổng {len(rows)} mạng[/bold green] · mạnh nhất: [bold]{best['ssid'][:20]}[/bold]")


# ----------------------------
# Network detection and testing
# ----------------------------

SPEED_BASE = "https://speed.cloudflare.com"
PING_TARGETS = [("1.1.1.1", 443), ("8.8.8.8", 443)]
DL_BYTES = 10_000_000
DL_URLS = [
    "https://speed.cloudflare.com/__down?bytes=10000000",
    "https://proof.ovh.net/files/10Mb.dat",
    "http://speedtest.tele2.net/10MB.zip",
]
DL_SECONDS = 8
UL_BYTES = 4_000_000
UA = "Mozilla/5.0 (Linux; Android) wireless-scanner"


def jcmd(name):
    cmd = get_cmd(name)
    if not cmd:
        return None
    rc, out, _ = run([cmd], timeout=10)
    if rc != 0:
        return None
    try:
        data = json.loads(out)
    except Exception:
        return None
    if isinstance(data, dict) and not data.get("API_ERROR"):
        return data
    return None


def detect_active():
    """Return (kind, label, detail) of active network."""
    wifi = jcmd("termux-wifi-connectioninfo")
    if wifi and wifi.get("supplicant_state") == "COMPLETED" and str(wifi.get("ip") or "") != "0.0.0.0":
        ssid = str(wifi.get("ssid") or "").strip('"')
        if not ssid or ssid.startswith("<unknown"):
            ssid = "(chưa cấp quyền Vị trí)"
        parts = []
        rssi = to_int(wifi.get("rssi"))
        if rssi is not None:
            parts.append(f"{rssi} dBm")
        freq = to_int(wifi.get("frequency_mhz"))
        if freq:
            parts.append(band(freq))
        link = to_int(wifi.get("link_speed_mbps"))
        if link:
            parts.append(f"link {link} Mbps")
        return "wifi", f"Wi‑Fi · {ssid}", " · ".join(parts)

    mobile = jcmd("termux-telephony-deviceinfo")
    if mobile and str(mobile.get("data_state", "")).lower() == "connected":
        op = str(mobile.get("network_operator_name") or mobile.get("sim_operator_name") or "").strip()
        nt = str(mobile.get("network_type") or "").upper()
        return "data", "Dữ liệu di động" + (f" · {op}" if op else ""), nt
    return "unknown", "Mạng không xác định", ""


def tcp_samples(host: str, port: int, n: int = 6):
    ms = []
    fails = 0
    for _ in range(n):
        start = time.perf_counter()
        try:
            socket.create_connection((host, port), timeout=3).close()
            ms.append((time.perf_counter() - start) * 1000)
        except OSError:
            fails += 1
        time.sleep(0.1)
    if not ms:
        return None
    jit = sum(abs(x - y) for x, y in zip(ms, ms[1:])) / max(1, len(ms) - 1)
    return {"avg": sum(ms) / len(ms), "jit": jit, "fail": fails, "n": n}


def test_latency(n: int = 6):
    for host, port in PING_TARGETS:
        result = tcp_samples(host, port, n)
        if result:
            return result
    return None


def conn_for():
    parsed = urlparse(SPEED_BASE)
    cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    return cls(parsed.hostname, parsed.port, timeout=10)


def download_from(url, progress=None):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=10) as resp:
        start = time.perf_counter()
        t0 = tl = None
        got = 0
        last = 0.0
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            now = time.perf_counter()
            if t0 is None:
                t0 = tl = now
                continue
            got += len(chunk)
            tl = now
            if now - last > 0.25 and tl > t0:
                last = now
                if progress is not None:
                    progress(got * 8 / (tl - t0) / 1e6)
            if now - start > DL_SECONDS:
                break
    if got == 0 or tl <= t0:
        raise ValueError("không nhận được dữ liệu")
    return got * 8 / (tl - t0) / 1e6


def test_download(progress=None):
    errs = []
    for url in DL_URLS:
        try:
            return download_from(url, progress), ""
        except urllib.error.HTTPError as exc:
            errs.append(f"{urlparse(url).hostname}: HTTP {exc.code}")
        except Exception as exc:
            errs.append(f"{urlparse(url).hostname}: {str(exc)[:40] or type(exc).__name__}")
    return None, errs[0] if errs else "không rõ lỗi"


def test_upload():
    try:
        data = os.urandom(UL_BYTES)
        conn = conn_for()
        conn.connect()
        begin = time.perf_counter()
        conn.request("POST", "/__up", body=data, headers={
            "User-Agent": UA,
            "Content-Type": "application/octet-stream",
        })
        conn.getresponse().read()
        elapsed = time.perf_counter() - begin
        conn.close()
        return UL_BYTES * 8 / elapsed / 1e6
    except Exception:
        return None


def meter(v, mx=100):
    n = 10
    fill = 0 if not v else min(n, max(1, round(v / mx * n)))
    return "[green]" + "█" * fill + "[dim]" + "░" * (n - fill) + "[/dim]"


def fmt_mbps(v):
    return "lỗi" if v is None else f"{v:.1f} Mbps"


def rating(res):
    if res.get("down") is None:
        return "Chưa đủ dữ liệu", "[cyan]"
    d = res["down"]
    l = res["lat"]["avg"]
    if d >= 50 and l < 50:
        return "Rất tốt", "[green]"
    if d >= 15 and l < 100:
        return "Tốt", "[green]"
    if d >= 5 and l < 200:
        return "Tạm được", "[yellow]"
    return "Yếu", "[red]"


def run_test():
    kind, label, detail = detect_active()
    if kind == "data":
        mb = (DL_BYTES + UL_BYTES) // 1_000_000
        choice = input(f"{YE}Test dùng tối đa ~{mb} MB dữ liệu. Enter để chạy, n để hủy: {R}").strip().lower()
        if choice == "n":
            return None
    result = {"kind": kind, "label": label, "detail": detail}
    print_line(f"\n[bold cyan]{label}[/bold cyan]")
    if detail:
        print_line(f"[dim]{detail}[/dim]")
    print_line("[green]Đang đo độ trễ...[/green]")
    result["lat"] = test_latency()
    if result["lat"] is None:
        print_line("[bold red][!] Không có kết nối internet.[/bold red]")
        return None
    result["down"], result["down_err"] = test_download(lambda v: print_line(f"[green]Đang đo tải xuống... {v:.1f} Mbps[/green]"))
    print_line("[green]Đang đo tải lên...[/green]")
    result["up"] = test_upload()
    append_history("network_test", label, detail, result)
    return result


def print_test_result(result):
    lat = result["lat"]
    table = Table(show_header=False, box=None)
    table.add_column("Key", style="cyan", min_width=12)
    table.add_column("Value", style="bold white")
    table.add_row("Độ trễ", f"{lat['avg']:.0f} ms ±{lat['jit']:.0f} ms")
    table.add_row("Tải xuống", fmt_mbps(result["down"]))
    table.add_row("Tải lên", fmt_mbps(result["up"]))
    txt, txt_style = rating(result)
    table.add_row("Đánh giá", f"{txt_style}{txt}[/]")
    print_line(Panel(table, title="[bold cyan]KẾT QUẢ TEST MẠNG[/bold cyan]", border_style="green"))
    if result.get("down") is None:
        print_line(f"[red]↳ {result.get('down_err')}[/red]")


def compare_flow():
    first = run_test()
    if not first:
        return
    if first["kind"] == "unknown":
        print_test_result(first)
        print_line("[yellow]Không xác định được loại mạng nên không so sánh được.[/yellow]")
        return
    tip = "Tắt Wi‑Fi để chuyển sang dữ liệu di động" if first["kind"] == "wifi" else "Bật Wi‑Fi và đợi kết nối"
    print_line(f"\n[bold yellow]Bước 2:[/bold yellow] {tip}")
    input("Xong rồi nhấn Enter...")
    if detect_active()[0] == first["kind"]:
        print_line("[red][!] Vẫn đang dùng cùng loại mạng, chưa so sánh được.[/red]")
        print_test_result(first)
        return
    second = run_test()
    if not second:
        return

    a, b = (first, second) if first["kind"] == "wifi" else (second, first)
    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("Chỉ số")
    table.add_column("Wi‑Fi")
    table.add_column("Dữ liệu")
    table.add_row("Độ trễ", f"{a['lat']['avg']:.0f} ms", f"{b['lat']['avg']:.0f} ms")
    table.add_row("Tải xuống", fmt_mbps(a["down"]), fmt_mbps(b["down"]))
    table.add_row("Tải lên", fmt_mbps(a["up"]), fmt_mbps(b["up"]))
    print_line(Panel(table, title="[bold cyan]SO SÁNH WI‑FI / DỮ LIỆU[/bold cyan]", border_style="cyan"))


# ----------------------------
# Website and IP checks
# ----------------------------

WEB_MAX_BYTES = 5_000_000
GEO_URL = "http://ip-api.com/json/{ip}?fields=status,country,regionName,city,isp,org&lang=vi"


def normalize_url(raw):
    raw = raw.strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = "https://" + raw
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    return raw


def resolve_host(host, port):
    start = time.perf_counter()
    infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    dns_ms = (time.perf_counter() - start) * 1000
    v4, v6 = [], []
    for fam, _, _, _, sockaddr in infos:
        lst = v4 if fam == socket.AF_INET else v6
        if sockaddr[0] not in lst:
            lst.append(sockaddr[0])
    return v4, v6, dns_ms


def geo_lookup(ip):
    try:
        req = urllib.request.Request(GEO_URL.format(ip=ip), headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode("utf-8", "replace"))
        return data if data.get("status") == "success" else None
    except Exception:
        return None


def web_fetch(url, hops=4):
    for _ in range(hops):
        parsed = urlparse(url)
        https = parsed.scheme == "https"
        cls = http.client.HTTPSConnection if https else http.client.HTTPConnection
        conn = cls(parsed.hostname, parsed.port, timeout=10)
        t0 = time.perf_counter()
        conn.connect()
        conn_ms = (time.perf_counter() - t0) * 1000
        tls = conn.sock.version() if https else None
        path = (parsed.path or "/") + ("?" + parsed.query if parsed.query else "")
        t1 = time.perf_counter()
        conn.request("GET", path, headers={
            "User-Agent": UA,
            "Accept": "*/*",
            "Accept-Encoding": "identity",
        })
        resp = conn.getresponse()
        ttfb = (time.perf_counter() - t1) * 1000
        location = resp.getheader("Location")
        if resp.status in (301, 302, 303, 307, 308) and location:
            conn.close()
            url = urljoin(url, location)
            continue
        got = after = 0
        first = last = None
        start = time.perf_counter()
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            now = time.perf_counter()
            got += len(chunk)
            if first is None:
                first = now
            else:
                after += len(chunk)
                last = now
            if got >= WEB_MAX_BYTES or now - start > DL_SECONDS:
                break
        total_ms = (time.perf_counter() - t1) * 1000
        speed = None
        if after >= 200_000 and last and last > first:
            speed = after * 8 / (last - first) / 1e6
        server = resp.getheader("Server") or ""
        conn.close()
        return {
            "status": resp.status,
            "reason": resp.reason,
            "server": server,
            "url": url,
            "conn_ms": conn_ms,
            "tls": tls,
            "ttfb": ttfb,
            "total_ms": total_ms,
            "size": got,
            "speed": speed,
        }
    raise ValueError("Quá nhiều lần chuyển hướng")


def human_size(n):
    return f"{n / 1e6:.1f} MB" if n >= 1e6 else f"{n / 1e3:.0f} KB"


def website_check():
    raw = input("\nNhập link website (vd: google.com): ").strip()
    url = normalize_url(raw)
    if not url:
        print_line("[yellow]Link không hợp lệ hoặc để trống.[/yellow]")
        return
    parsed = urlparse(url)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    print_line("[green]Đang kiểm tra mạng đang dùng...[/green]")
    kind, label, _ = detect_active()
    print_line("[green]Đang tra IP của host...[/green]")
    try:
        v4, v6, dns_ms = resolve_host(host, port)
    except socket.gaierror:
        print_line(f"[bold red][!] Không phân giải được tên miền {host}.[/bold red]")
        return
    ip = (v4 or v6)[0]
    geo = geo_lookup(ip)
    ping = tcp_samples(ip, port, 5)
    try:
        web = web_fetch(url)
    except ssl.SSLError as exc:
        web_err = f"Lỗi chứng chỉ SSL: {str(exc)[:40]}"
        web = None
    except Exception as exc:
        web_err = f"{str(exc)[:50] or type(exc).__name__}"
        web = None

    table = Table(show_header=False, box=None)
    table.add_column("Key", style="cyan", min_width=12)
    table.add_column("Value", style="bold white")
    table.add_row("Mạng", label)
    table.add_row("Host", host)
    table.add_row("IP", ip)
    if geo:
        place = ", ".join(x for x in (geo.get("city"), geo.get("country")) if x)
        table.add_row("Vị trí", place or "-")
        table.add_row("Nhà mạng", (geo.get("isp") or geo.get("org") or "-")[:26])
    table.add_row("DNS", f"{dns_ms:.0f} ms")
    if ping:
        table.add_row("Ping TCP", f"{ping['avg']:.0f} ms ±{ping['jit']:.0f} ms")
    else:
        table.add_row("Ping TCP", "không phản hồi")
    if web:
        table.add_row("Kết nối", f"{web['conn_ms']:.0f} ms")
        table.add_row("TTFB", f"{web['ttfb']:.0f} ms")
        table.add_row("Tải trang", human_size(web["size"]))
        if web["speed"] is not None:
            table.add_row("Tốc độ", fmt_mbps(web["speed"]))
        else:
            table.add_row("Tốc độ", "-")
        table.add_row("HTTP", f"{web['status']} {web['reason']}")
    else:
        table.add_row("HTTP", f"[red]{web_err}[/red]")
    print_line(Panel(table, title="[bold cyan]KẾT QUẢ TRA WEBSITE[/bold cyan]", border_style="cyan"))
    append_history("website", host, label, {"url": url, "ip": ip, "geo": geo, "web": web})


# ----------------------------
# SMS/call scheduling
# ----------------------------

MODE_TXT = {"call": "Gọi bằng SIM", "sms": "Nhắn SMS", "both": "Nhắn SMS rồi gọi"}


def fmt_countdown(sec):
    s = max(0, int(sec))
    days, remainder = divmod(s, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    return (f"{days} ngày " if days else "") + f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def normalize_phone(raw):
    p = re.sub(r"[\s.\-()]", "", raw)
    if p.startswith("0"):
        p = "+84" + p[1:]
    return p if re.fullmatch(r"\+?\d{8,15}", p) else None


def parse_when(txt):
    txt = txt.strip()
    now = datetime.now()
    try:
        if len(txt) <= 5:
            h, m = map(int, txt.split(":"))
            target = now.replace(hour=h, minute=m, second=0, microsecond=0)
            return (target + timedelta(days=1) if target <= now else target), True
        target = datetime.strptime(txt, "%Y-%m-%d %H:%M")
        return (target if target > now else None), False
    except ValueError:
        return None, False


def do_action(mode, to, message):
    if mode in ("sms", "both"):
        rc, _, err = run(["termux-sms-send", "-n", to, message], timeout=20)
        print_line(f"  {'[green]✓ Đã nhắn SMS[/green]' if rc == 0 else f'[red]✗ Lỗi SMS: {err}[/red]'}")
        if mode == "both":
            time.sleep(5)
    if mode in ("call", "both"):
        rc, _, err = run(["termux-telephony-call", to], timeout=20)
        print_line(f"  {'[green]✓ Đã gọi bằng SIM[/green]' if rc == 0 else f'[red]✗ Lỗi gọi: {err}[/red]'}")


def schedule_task():
    print_line(Panel("Nhập số điện thoại và thời điểm hẹn giờ", title="[bold cyan]HẸN GIỜ GỌI / SMS[/bold cyan]", border_style="cyan"))
    recipient = normalize_phone(input("Số điện thoại người nhận: "))
    if not recipient:
        print_line("[yellow]Số điện thoại không hợp lệ.[/yellow]")
        return

    mode_map = {"1": "call", "2": "sms", "3": "both"}
    mode_choice = input("Hình thức [1=Gọi, 2=SMS, 3=SMS rồi gọi]: ").strip()
    mode = mode_map.get(mode_choice)
    if not mode:
        print_line("[yellow]Hình thức không hợp lệ.[/yellow]")
        return

    message = ""
    if mode != "call":
        message = input("Nội dung tin nhắn: ").strip()
        if not message:
            print_line("[yellow]Chưa nhập nội dung tin nhắn.[/yellow]")
            return

    when, can_repeat = parse_when(input("Giờ (07:30 hoặc 2026-10-16 07:30): "))
    if not when:
        print_line("[yellow]Giờ không hợp lệ hoặc đã qua.[/yellow]")
        return
    daily = can_repeat and input("Lặp lại mỗi ngày? [y/N]: ").strip().lower() == "y"

    required = {"call": ["termux-telephony-call"], "sms": ["termux-sms-send"], "both": ["termux-sms-send", "termux-telephony-call"]}[mode]
    if [cmd for cmd in required if not get_cmd(cmd)]:
        print_line("[bold red][!] Thiếu lệnh Termux:API cần cho chức năng này.[/bold red]")
        offer_install()
        return

    print_line(f"[bold]{MODE_TXT[mode]}[/bold] tới [bold]{recipient}[/bold]")
    print_line(f"Lúc [bold]{when:%H:%M %d/%m/%Y}[/bold]{' (lặp mỗi ngày)' if daily else ''}")
    if message:
        print_line(f"Tin nhắn: [dim]{message[:30]}[/dim]")
    input("Nhấn Enter để bắt đầu đếm ngược...")

    if get_cmd("termux-wake-lock"):
        run(["termux-wake-lock"], timeout=5)
    print_line("[green]Giữ Termux mở. Nhấn CTRL+C để hủy.[/green]")
    target = when
    try:
        while True:
            while True:
                remaining = (target - datetime.now()).total_seconds()
                if remaining <= 0:
                    break
                print_line(f"[green]Còn {fmt_countdown(remaining)} · {recipient}[/green]", end="\r")
                time.sleep(min(1, remaining))
            print_line(f"\n[bold][{datetime.now():%H:%M:%S}] Đến giờ![/bold]")
            do_action(mode, recipient, message)
            if not daily:
                break
            target += timedelta(days=1)
            print_line(f"[dim]Lần tiếp theo: {target:%H:%M %d/%m}[/dim]")
    except KeyboardInterrupt:
        print_line("[yellow]Đã hủy hẹn giờ.[/yellow]")
    finally:
        if get_cmd("termux-wake-unlock"):
            run(["termux-wake-unlock"], timeout=5)


# ----------------------------
# Extras: history and settings
# ----------------------------


def show_history():
    history = get_history(limit=8)
    if not history:
        print_line("[yellow]Chưa có lịch sử nào.[/yellow]")
        return
    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("Thời gian")
    table.add_column("Loại")
    table.add_column("Chú thích")
    for row in history:
        table.add_row(row["created_at"], row["kind"], row["label"] or row["detail"] or "-")
    print_line(Panel(table, title="[bold cyan]LỊCH SỬ GẦN ĐÂY[/bold cyan]", border_style="green"))


def settings_panel():
    cfg = load_config()
    print_line(Panel("Cấu hình ứng dụng ở ~/.config/wireless_scanner/config.json", title="[bold cyan]CÀU HÌNH[/bold cyan]", border_style="cyan"))
    table = Table(show_header=False, box=None)
    table.add_column("Key", style="cyan")
    table.add_column("Value", style="white")
    table.add_row("theme", cfg.get("theme", "default"))
    table.add_row("auto_notify", str(cfg.get("auto_notify", True)))
    table.add_row("show_advanced", str(cfg.get("show_advanced", True)))
    table.add_row("history_limit", str(cfg.get("history_limit", 50)))
    table.add_row("download_url", cfg.get("download_url", "https://speed.cloudflare.com/__down?bytes=10000000"))
    print_line(table)
    action = input("Bạn muốn cập nhật? [y/N]: ").strip().lower()
    if action != "y":
        return
    cfg["auto_notify"] = input("Tự động thông báo? [Y/n]: ").strip().lower() not in ("n", "N", "no")
    cfg["history_limit"] = int(input("Số bản ghi lịch sử (mặc định 50): ") or cfg.get("history_limit", 50))
    cfg["download_url"] = input("URL download mặc định: ") or cfg.get("download_url", "https://speed.cloudflare.com/__down?bytes=10000000")
    save_config(cfg)
    print_line("[green]Đã lưu cấu hình.[/green]")


# ----------------------------
# Comprehensive health checks
# ----------------------------


def check_tools():
    print_line(Panel("Kiểm tra công cụ Termux API", title="[bold cyan]KIỂM TRA CÔNG CỤ[/bold cyan]", border_style="cyan"))
    rows = [
        ("termux-wifi-scaninfo", "Quét Wi‑Fi", "Vị trí"),
        ("termux-wifi-connectioninfo", "Nhận diện Wi‑Fi", "Vị trí"),
        ("termux-telephony-deviceinfo", "Nhận diện dữ liệu", "Điện thoại"),
    ]
    results = []
    any_missing = False
    for name, title, perm in rows:
        status = probe(name, 8)
        if status == "ok":
            results.append((title, "[green]OK[/green]", "Khả dụng"))
        elif status == "missing":
            results.append((title, "[red]MISSING[/red]", "Thiếu termux-api"))
            any_missing = True
        elif status == "perm":
            results.append((title, "[yellow]PERM[/yellow]", f"Cấp quyền {perm}"))
        else:
            results.append((title, "[yellow]APP OFF[/yellow]", "App Termux:API chưa mở"))
    for name, title in (("termux-telephony-call", "Gọi bằng SIM"), ("termux-sms-send", "Nhắn SMS")):
        ok = bool(get_cmd(name))
        any_missing = any_missing or not ok
        results.append((title, "[green]OK[/green]" if ok else "[red]MISSING[/red]", ""))
    print_line(render_status_table(results))
    if any_missing:
        offer_install()
    else:
        print_line("[dim]Hệ thống đã sẵn sàng cho hầu hết chức năng.[/dim]")


# ----------------------------
# Installation guide
# ----------------------------


def install_guide():
    clear()
    print_line(Panel("Hướng dẫn cài đặt Termux", title="[bold cyan]HƯỚNG DẪN CÀI ĐẶT[/bold cyan]", border_style="cyan"))
    steps = [
        "Bước 1: Cài Termux và Termux:API từ cùng một nguồn (F-Droid hoặc GitHub).",
        "Bước 2: Cài Python: pkg install -y python",
        "Bước 3: Trong Termux: termux-setup-storage; cd ~/storage/downloads; python wireless_scanner.py",
        "Bước 4: Cấp quyền cho Termux:API: Vị trí, Điện thoại, SMS.",
        "Bước 5: Giữ app chạy nền để hẹn giờ gọi/SMS: Cài đặt > Pin > Termux > Không hạn chế.",
    ]
    for i, step in enumerate(steps, 1):
        print_line(f"[bold cyan]{i}.[/bold cyan] {step}")
    print_line("[yellow]Nếu gặp lỗi command not found, hãy chạy mục 7 hoặc pkg install -y termux-api.[/yellow]")


# ----------------------------
# Main menu loop
# ----------------------------


def splash_screen():
    if console is not None:
        with Live(render_splash(), refresh_per_second=4, transient=False) as live:
            for _ in range(3):
                time.sleep(0.3)
                live.update(render_splash())
    else:
        print_line(render_splash())
    status = check_termux_state()
    if status["missing"] or not status["app"] or not status["perm"]:
        print_line("[yellow]Một số chức năng sẽ bị hạn chế nếu Termux:API chưa sẵn sàng.[/yellow]")
    else:
        print_line("[bold green]Sẵn sàng.[/bold green]")


def main():
    ensure_dirs()
    init_history_db()
    splash_screen()
    while True:
        clear()
        if console is not None:
            console.print(render_menu())
        else:
            print_line(render_menu())
        choice = input("\nChọn [1-9]: ").strip()
        if choice == "1":
            rows, err = scan_wifi()
            if err:
                print_line(f"[bold red][!] {err}[/bold red]")
            else:
                display_wifi(rows)
            append_history("wifi_scan", "Wi‑Fi", f"{len(rows)} mạng", {"count": len(rows)})
        elif choice == "2":
            result = run_test()
            if result:
                print_test_result(result)
        elif choice == "3":
            compare_flow()
        elif choice == "4":
            website_check()
        elif choice == "5":
            schedule_task()
        elif choice == "6":
            show_history()
            settings_panel()
        elif choice == "7":
            check_tools()
        elif choice == "8":
            install_guide()
        elif choice == "9":
            print_line("[green]Tạm biệt.[/green]")
            break
        else:
            print_line("[yellow]Lựa chọn không hợp lệ.[/yellow]")
        input("\nNhấn Enter để tiếp tục...")


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print_line("\n\n[green]Đã dừng.[/green]")
    except Exception as exc:
        print_line(f"\n[bold red]Lỗi: {exc}[/bold red]")
        raise
