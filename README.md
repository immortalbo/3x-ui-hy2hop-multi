# 3x-ui Hysteria2 Multi-Hop Sync

自动读取 [MHSanaei/3x-ui](https://github.com/MHSanaei/3x-ui) 数据库，为所有启用且配置了 **Hop Ports** 的 Hysteria2 入站同步 nftables UDP 端口跳跃规则。

适用于同时维护多个 Hysteria2 入站的场景。新增、修改、禁用或删除入站后，无需手工维护 DNAT/redirect 规则。

## 功能

- 只读访问 `/etc/x-ui/x-ui.db`，不修改 3x-ui 数据库。
- 自动发现所有启用的 `protocol=hysteria` 入站。
- 读取 `finalmask.quicParams.udpHop.ports` 中的 Hop Ports。
- 每 15 秒自动对账，新增、更新或删除对应 nftables 规则。
- 同时支持多个 Hysteria2 入站和多组端口范围。
- IPv4 必定配置；内核支持 IPv6 NAT 时同时配置 IPv6。
- 使用独立 nftables 表，不清空或接管系统其他规则。
- 使用单次 nftables 事务原子更新，失败时保留上一份可用规则。
- 配置未变化时不重建规则、不清零计数。
- 检测 Hop 范围重叠、监听端口未启动、格式错误及指定 IP 监听。
- 提供状态检查、手动同步和完整恢复命令。

## 工作原理

假设 3x-ui 中存在以下入站：

```text
ID=3  UDP 20000-30000 -> 443
ID=4  UDP 31000-32000 -> 8443
```

同步器会创建：

```nft
udp dport 20000-30000 redirect to :443
udp dport 31000-32000 redirect to :8443
```

仅重定向目的地址属于本机的 UDP 流量：

```nft
fib daddr type local
```

因此不会匹配经本机转发到其他目标的流量。

## 系统要求

- 使用 systemd 的 Linux。
- Python 3.8+，包含 `sqlite3` 模块。
- nftables、`ss` 和 systemctl。
- 标准脚本安装的 3x-ui，数据库路径为 `/etc/x-ui/x-ui.db`。
- 建议使用 KVM VPS；受限 OpenVZ/LXC 可能不支持 nftables NAT。

目前主要针对 Debian 11/12/13 和 Ubuntu 20.04/22.04/24.04/26.04。

## 安装

```bash
tmp_file="$(mktemp)"
curl -fsSL https://raw.githubusercontent.com/immortalbo/3x-ui-hy2hop-multi/main/hy2hop_multi.py -o "$tmp_file"
sudo python3 "$tmp_file" install
rm -f "$tmp_file"
```

安装后会创建：

```text
/usr/local/sbin/hcloudlab-hy2hop-multi
/etc/systemd/system/hcloudlab-hy2hop-multi.service
/etc/systemd/system/hcloudlab-hy2hop-multi.timer
/var/lib/hcloudlab-hy2hop-multi/state.json
```

## 使用

查看当前数据库配置和 nftables 规则：

```bash
sudo /usr/local/sbin/hcloudlab-hy2hop-multi status
```

立即同步：

```bash
sudo /usr/local/sbin/hcloudlab-hy2hop-multi sync
```

查看定时器：

```bash
systemctl status hcloudlab-hy2hop-multi.timer
systemctl list-timers hcloudlab-hy2hop-multi.timer
```

查看同步日志：

```bash
journalctl -u hcloudlab-hy2hop-multi.service
```

卸载并删除本工具创建的 nftables 表和 systemd 单元：

```bash
sudo /usr/local/sbin/hcloudlab-hy2hop-multi restore
```

卸载不会修改 3x-ui 数据库，也不会卸载 nftables。

## 3x-ui 配置

在 Hysteria2 入站中设置：

```text
Stream Settings -> FinalMask -> QUIC Params -> UDP Hop -> Ports
```

例如：

```text
20000-30000
```

不同入站的 Hop Ports 不得重叠，实际监听端口不能位于自己的 Hop 范围内。

## 防火墙与云安全组

本项目只维护 nftables 端口重定向规则，不修改 UFW、firewalld 或云厂商安全组。

你仍需在 VPS 服务商控制台放行：

```text
Hysteria2 实际监听端口/UDP
全部 Hop Ports 范围/UDP
```

例如：

```text
UDP 443
UDP 20000-30000
```

不要误放行成 TCP。

## 安全策略

- 数据库以 SQLite 只读 URI 打开。
- 新配置校验失败时不覆盖现有可用规则。
- 目标 UDP 端口未监听时拒绝同步。
- 多个 Hop 范围重叠时拒绝同步。
- 所有规则位于独立的 `hcloudlab_hy2hop_multi` 表。
- 状态文件权限为 `0600`。

## License

[MIT](LICENSE)

## 致谢

本项目的需求与安全边界设计参考了
[hcloudlab/Hysteria2-Hop](https://github.com/hcloudlab/Hysteria2-Hop)，并扩展为多入站数据库自动对账模式。
