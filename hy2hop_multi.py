#!/usr/bin/env python3
"""Keep nftables UDP-hop redirects in sync with all 3x-ui Hysteria2 inbounds."""

import argparse
import contextlib
import fcntl
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

DB = Path("/etc/x-ui/x-ui.db")
SELF = Path("/usr/local/sbin/hcloudlab-hy2hop-multi")
SERVICE = "hcloudlab-hy2hop-multi.service"
TIMER = "hcloudlab-hy2hop-multi.timer"
SERVICE_FILE = Path("/etc/systemd/system") / SERVICE
TIMER_FILE = Path("/etc/systemd/system") / TIMER
STATE_DIR = Path("/var/lib/hcloudlab-hy2hop-multi")
STATE = STATE_DIR / "state.json"
LOCK = Path("/run/hcloudlab-hy2hop-multi.lock")
TABLE = "hcloudlab_hy2hop_multi"
ENV = dict(os.environ, LC_ALL="C", LANG="C", PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")


def run(cmd, check=True, input_text=None):
    p = subprocess.run(cmd, input=input_text, text=True, capture_output=True, env=ENV)
    if check and p.returncode:
        raise RuntimeError(f"{' '.join(cmd)}: {p.stderr.strip() or p.stdout.strip()}")
    return p


def require_root():
    if os.geteuid():
        raise RuntimeError("需要 root 权限")
    for command in ("nft", "systemctl", "ss"):
        if not shutil.which(command):
            raise RuntimeError(f"缺少命令: {command}")
    if not DB.is_file():
        raise RuntimeError(f"未找到 3x-ui 数据库: {DB}")


def parse_hop(raw):
    data = json.loads(raw or "{}")
    value = data.get("finalmask", {}).get("quicParams", {}).get("udpHop", {}).get("ports")
    if not value:
        return None
    match = re.fullmatch(r"\s*(\d+)\s*-\s*(\d+)\s*", str(value))
    if not match:
        raise ValueError(f"Hop Ports 格式无效: {value!r}")
    start, end = map(int, match.groups())
    if not (1 <= start <= end <= 65535):
        raise ValueError(f"Hop Ports 超出范围: {value!r}")
    return start, end


def desired():
    query = ("SELECT id, remark, port, listen, stream_settings FROM inbounds "
             "WHERE enable=1 AND protocol='hysteria' ORDER BY id")
    configs = []
    with contextlib.closing(sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=10)) as con:
        for ident, remark, port, listen, raw in con.execute(query):
            try:
                hop = parse_hop(raw)
                port = int(port)
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"入站 {ident} 配置无效: {exc}") from None
            if hop is None:
                continue
            start, end = hop
            if not 1 <= port <= 65535:
                raise RuntimeError(f"入站 {ident} 监听端口无效: {port}")
            if start <= port <= end:
                raise RuntimeError(f"入站 {ident} 的 Hop 范围包含监听端口 {port}")
            if listen not in (None, "", "::", "0.0.0.0"):
                raise RuntimeError(f"入站 {ident} 绑定了指定地址 {listen!r}，暂不支持")
            configs.append({"id": ident, "remark": remark or "", "port": port,
                            "start": start, "end": end})
    for i, left in enumerate(configs):
        for right in configs[i + 1:]:
            if left["start"] <= right["end"] and right["start"] <= left["end"]:
                raise RuntimeError(f"Hop 范围重叠: 入站 {left['id']} 与入站 {right['id']}")
    return configs


def udp_listeners():
    p = run(["ss", "-H", "-lun"], False)
    if p.returncode:
        raise RuntimeError("无法读取 UDP 监听端口")
    ports = set()
    for line in p.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 5:
            match = re.search(r":(\d+)$", fields[3])
            if match:
                ports.add(int(match.group(1)))
    return ports


def table_exists(family):
    return run(["nft", "list", "table", family, TABLE], False).returncode == 0


def ipv6_supported(configs):
    if not Path("/proc/net/if_inet6").exists():
        return False
    probe = render(configs, ["ip6"], ["ip6"] if table_exists("ip6") else [])
    return run(["nft", "-c", "-f", "-"], False, probe).returncode == 0


def render(configs, families, existing):
    lines = [f"delete table {family} {TABLE}" for family in existing]
    for family in families:
        lines.extend([
            f"add table {family} {TABLE}",
            f"add chain {family} {TABLE} prerouting {{ type nat hook prerouting priority -100; policy accept; }}",
        ])
        for cfg in configs:
            comment = f"3x-ui inbound {cfg['id']}"
            lines.append(
                f"add rule {family} {TABLE} prerouting fib daddr type local "
                f"udp dport {cfg['start']}-{cfg['end']} counter redirect to :{cfg['port']} "
                f"comment \"{comment}\""
            )
    return "\n".join(lines) + ("\n" if lines else "")


def write_state(configs, families):
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".state-", dir=STATE_DIR)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump({"configs": configs, "families": families}, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, 0o600)
        os.replace(name, STATE)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name)


def rules_match(configs, families):
    expected = set(families)
    present = {family for family in ("ip", "ip6") if table_exists(family)}
    if present != expected:
        return False
    for family in families:
        output = run(["nft", "list", "table", family, TABLE], False).stdout
        rule_lines = [line for line in output.splitlines() if "3x-ui inbound " in line]
        if len(rule_lines) != len(configs):
            return False
        for cfg in configs:
            if not any(f"udp dport {cfg['start']}-{cfg['end']}" in line
                       and f"redirect to :{cfg['port']}" in line
                       and f'3x-ui inbound {cfg["id"]}' in line for line in rule_lines):
                return False
    return True


def sync():
    configs = desired()
    listeners = udp_listeners()
    missing = [cfg for cfg in configs if cfg["port"] not in listeners]
    if missing:
        details = ", ".join(f"ID={cfg['id']} UDP:{cfg['port']}" for cfg in missing)
        raise RuntimeError(f"目标端口未监听，保留现有规则不变: {details}")
    families = ["ip"]
    if configs and ipv6_supported(configs):
        families.append("ip6")
    active_families = families if configs else []
    old_state = None
    if STATE.exists():
        with contextlib.suppress(OSError, ValueError, json.JSONDecodeError):
            old_state = json.loads(STATE.read_text())
    if old_state == {"configs": configs, "families": active_families} and rules_match(configs, active_families):
        print(f"配置无变化：{len(configs)} 个 Hysteria2 入站")
        return
    existing = [family for family in ("ip", "ip6") if table_exists(family)]
    candidate = render(configs, active_families, existing)
    if candidate:
        check = run(["nft", "-c", "-f", "-"], False, candidate)
        if check.returncode:
            raise RuntimeError(f"nftables 校验失败，保留现有规则: {check.stderr.strip()}")
        run(["nft", "-f", "-"], input_text=candidate)
    write_state(configs, active_families)
    print(f"已同步 {len(configs)} 个 Hysteria2 入站")
    for cfg in configs:
        print(f"  ID={cfg['id']} {cfg['remark']} UDP {cfg['start']}-{cfg['end']} -> {cfg['port']}")


def service_text():
    return f"""[Unit]
Description=Synchronize all 3x-ui Hysteria2 UDP hop rules
After=network-online.target x-ui.service
Wants=network-online.target

[Service]
Type=oneshot
ExecStart={SELF} sync
"""


def timer_text():
    return """[Unit]
Description=Periodically synchronize 3x-ui Hysteria2 UDP hop rules

[Timer]
OnBootSec=15s
OnUnitActiveSec=15s
AccuracySec=2s
Persistent=true
Unit=hcloudlab-hy2hop-multi.service

[Install]
WantedBy=timers.target
"""


def install():
    source = Path(__file__).resolve()
    if source != SELF:
        tmp = SELF.with_name("." + SELF.name + ".new")
        shutil.copyfile(source, tmp)
        tmp.chmod(0o755)
        os.replace(tmp, SELF)
    SERVICE_FILE.write_text(service_text())
    TIMER_FILE.write_text(timer_text())
    run(["systemctl", "daemon-reload"])
    sync()
    run(["systemctl", "enable", "--now", TIMER])
    print("安装完成：每 15 秒自动同步全部 Hysteria2 Hop 入站")


def status():
    configs = desired()
    print(f"数据库中有 {len(configs)} 个启用且配置 Hop Ports 的 Hysteria2 入站")
    for cfg in configs:
        print(f"  ID={cfg['id']} {cfg['remark']} UDP {cfg['start']}-{cfg['end']} -> {cfg['port']}")
    for family in ("ip", "ip6"):
        p = run(["nft", "list", "table", family, TABLE], False)
        if p.returncode == 0:
            print(f"\n[{family}]\n{p.stdout.strip()}")
    timer = run(["systemctl", "is-active", TIMER], False).stdout.strip()
    print(f"\n定时器状态: {timer or 'inactive'}")


def restore():
    run(["systemctl", "disable", "--now", TIMER], False)
    for family in ("ip", "ip6"):
        if table_exists(family):
            run(["nft", "delete", "table", family, TABLE])
    SERVICE_FILE.unlink(missing_ok=True)
    TIMER_FILE.unlink(missing_ok=True)
    run(["systemctl", "daemon-reload"])
    STATE.unlink(missing_ok=True)
    with contextlib.suppress(OSError):
        STATE_DIR.rmdir()
    SELF.unlink(missing_ok=True)
    print("恢复完成")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("install", "sync", "status", "restore"))
    args = parser.parse_args()
    require_root()
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with LOCK.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        {"install": install, "sync": sync, "status": status, "restore": restore}[args.action]()


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, sqlite3.Error, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
