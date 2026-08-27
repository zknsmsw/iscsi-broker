#!/usr/bin/env python3
"""netctrl.py 逻辑测试（不依赖 root / iptables，Windows 上可运行）。"""
import os, sys, tempfile, shutil
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import netctrl

_failures = []
_TMP_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".test_tmp")

def _tmp():
    os.makedirs(_TMP_ROOT, exist_ok=True)
    return tempfile.mkdtemp(dir=_TMP_ROOT)

def check(name, cond):
    if cond:
        print(f"  ok  {name}")
    else:
        _failures.append(name)
        print(f"FAIL  {name}")

def reset(tmp, default="allow", macs=None, lan="lan0", wan="wan0"):
    netctrl._ready = False
    netctrl._ifs = (lan, wan)
    netctrl._cfg = {"default": default, "macs": dict(macs or {})}
    netctrl._recent_boots = {}
    netctrl._last_fp = None
    netctrl._base_dir = tmp
    netctrl._backup_dir = os.path.join(tmp, "netctrl_backup")
    netctrl._full_takeover = True
    netctrl._reject = True
    netctrl._cfg_lan = lan
    netctrl._cfg_wan = wan

def fake_runner(cmds, neigh_out=""):
    """替换防火墙执行函数，记录命令；返回恢复函数。"""
    orig_sh, orig_out = netctrl._sh, netctrl._out
    netctrl._sh = lambda args, timeout=5: cmds.append(args)
    netctrl._out = lambda args, timeout=5: neigh_out
    def restore():
        netctrl._sh, netctrl._out = orig_sh, orig_out
    return restore

def test_normalize_mac():
    print("[normalize_mac]")
    check("冒号大写", netctrl.normalize_mac("AA:BB:CC:DD:EE:FF") == "aabbccddeeff")
    check("短横线", netctrl.normalize_mac("AA-BB-CC-DD-EE-FF") == "aabbccddeeff")
    check("裸12位", netctrl.normalize_mac("aabbccddeeff") == "aabbccddeeff")
    check("非法字符", netctrl.normalize_mac("zz:bb:cc:dd:ee:ff") is None)
    check("空串", netctrl.normalize_mac("") is None)
    check("位数不足", netctrl.normalize_mac("aabbccddee") is None)

def test_effective():
    print("[effective]")
    reset(_tmp(), default="allow", macs={"001122334455": "deny"})
    check("显式deny", netctrl.effective("001122334455") == "deny")
    check("默认allow", netctrl.effective("667788990011") == "allow")
    reset(_tmp(), default="deny", macs={"001122334455": "allow"})
    check("显式allow", netctrl.effective("001122334455") == "allow")
    check("默认deny", netctrl.effective("667788990011") == "deny")

def test_config_roundtrip():
    print("[config parse/serialize]")
    text = "default=deny\n001122334455=allow\naabbccddeeff=deny\n# comment\n\n"
    cfg = netctrl._parse_conf(text)
    check("default解析", cfg["default"] == "deny")
    check("macs解析", cfg["macs"] == {"001122334455": "allow", "aabbccddeeff": "deny"})
    check("序列化往返", netctrl._parse_conf(netctrl._serialize_cfg(cfg)) == cfg)
    check("非法行忽略", netctrl._parse_conf("badmac=allow\n").get("macs") == {})
    check("非法策略忽略", netctrl._parse_conf("001122334455=maybe\n").get("macs") == {})
    check("大小写归一", netctrl._parse_conf("AA:BB:CC:DD:EE:FF=DENY\n").get("macs") == {"aabbccddeeff": "deny"})

def test_sync_rules_default_allow():
    print("[sync default=allow]")
    reset(_tmp(), default="allow", macs={"001122334455": "deny"})
    cmds = []
    restore = fake_runner(cmds)
    try:
        netctrl._sync_locked()
        mac_rules = [c for c in cmds if "--mac-source" in c]
        check("deny规则1条(冒号格式)", len(mac_rules) == 1 and mac_rules[0][-1] == "REJECT"
              and mac_rules[0][mac_rules[0].index("--mac-source") + 1] == "00:11:22:33:44:55")
        fallback = [c for c in cmds if c[:3] == ["iptables", "-A", "NETCTRL"] and "--mac-source" not in c]
        check("兜底ACCEPT", len(fallback) == 1 and fallback[0][-1] == "ACCEPT")
        hook = [c for c in cmds if c[:3] == ["iptables", "-I", "FORWARD"]]
        check("钩子插FORWARD第1条", len(hook) == 1 and hook[0][3] == "1")
        n0 = len(cmds)
        netctrl._sync_locked()
        check("指纹未变化跳过重建", len(cmds) == n0)
    finally:
        restore()

def test_sync_rules_default_deny():
    print("[sync default=deny]")
    reset(_tmp(), default="deny", macs={"001122334455": "allow"})
    cmds = []
    restore = fake_runner(cmds)
    try:
        netctrl._sync_locked()
        mac_rules = [c for c in cmds if "--mac-source" in c]
        check("allow规则1条", len(mac_rules) == 1 and mac_rules[0][-1] == "ACCEPT")
        fallback = [c for c in cmds if c[:3] == ["iptables", "-A", "NETCTRL"] and "--mac-source" not in c]
        check("兜底REJECT", len(fallback) == 1 and fallback[0][-1] == "REJECT")
    finally:
        restore()

def test_reject_drop_switch():
    print("[REJECT/DROP 开关]")
    reset(_tmp(), default="deny", macs={})
    netctrl._reject = False
    cmds = []
    restore = fake_runner(cmds)
    try:
        netctrl._sync_locked()
        fallback = [c for c in cmds if c[:3] == ["iptables", "-A", "NETCTRL"] and "--mac-source" not in c]
        check("DROP兜底", len(fallback) == 1 and fallback[0][-1] == "DROP")
    finally:
        restore()

def test_set_mac_and_remove():
    print("[set/remove mac]")
    reset(_tmp())
    netctrl._ready = True
    orig_sync = netctrl._sync_locked
    calls = []
    netctrl._sync_locked = lambda: calls.append("sync")
    try:
        msg = netctrl.set_mac("AA:BB:CC:DD:EE:FF", "deny")
        check("set返回消息", "已设为禁止联网" in msg)
        check("已写入配置", netctrl._cfg["macs"].get("aabbccddeeff") == "deny")
        check("触发同步", len(calls) == 1)
        msg = netctrl.remove_mac("aabbccddeeff")
        check("remove返回消息", "恢复默认" in msg)
        check("已删除配置", "aabbccddeeff" not in netctrl._cfg["macs"])
        check("非法MAC", netctrl.set_mac("bad", "allow").startswith("MAC"))
    finally:
        netctrl._sync_locked = orig_sync

def test_on_boot_and_reconcile():
    print("[on_boot / reconcile]")
    reset(_tmp())
    netctrl._ready = True
    cmds = []
    restore = fake_runner(cmds, "10.0.1.5 dev lan0 lladdr 00:11:22:33:44:55 REACHABLE\n")
    try:
        netctrl.on_client_boot("001122334455")
        check("开机记入缓存", "001122334455" in netctrl._recent_boots)
        check("开机触发规则", any("--mac-source" in c for c in cmds))
        cmds.clear()
        netctrl._recent_boots.clear()
        netctrl.reconcile_once(["aabbccddeeff"])
        check("巡检合并连接+邻居表", any("--mac-source" in c and "aa:bb:cc:dd:ee:ff" in c for c in cmds))
    finally:
        restore()

def test_is_unicast():
    print("[is_unicast]")
    check("单播", netctrl._is_unicast("001122334455") is True)
    check("组播(首字节奇数)", netctrl._is_unicast("333300000001") is False)
    check("组播(0x11奇数)", netctrl._is_unicast("112233445566") is False)
    check("广播", netctrl._is_unicast("ffffffffffff") is False)
    check("空/非法", netctrl._is_unicast("") is False and netctrl._is_unicast(None) is False)

def test_mac_colon():
    print("[mac_colon]")
    check("冒号格式化", netctrl._mac_colon("001122334455") == "00:11:22:33:44:55")
    check("全零", netctrl._mac_colon("000000000000") == "00:00:00:00:00:00")

def test_ignore_multicast_neighbor():
    print("[ignore multicast neighbor]")
    reset(_tmp(), default="allow", macs={})
    netctrl._ready = True
    cmds = []
    # 邻居表同时出现 IPv6 组播(33:33:...) 和一台真实客户机单播 MAC
    out = ("fe80::1 dev lan0 lladdr 33:33:00:00:00:01 router REACHABLE\n"
           "10.0.1.5 dev lan0 lladdr 00:11:22:33:44:55 REACHABLE\n")
    restore = fake_runner(cmds, out)
    try:
        netctrl._sync_locked()
        mac_rules = [c for c in cmds if "--mac-source" in c]
        check("只生成单播规则", len(mac_rules) == 1
              and "00:11:22:33:44:55" in mac_rules[0])
        check("不生成组播规则", not any("333300000001" in c or "33:33:00:00:00:01" in c for c in cmds))
    finally:
        restore()

def test_build_base_rules():
    print("[build_base_rules]")
    cmds = []
    restore = fake_runner(cmds)
    try:
        netctrl._build_base_rules("ens18", "ens19")
        ret = [c for c in cmds if c[:3] == ["iptables", "-A", "FORWARD"] and "conntrack" in c]
        check("NAT回程放行(lan/wan正确)", len(ret) == 1 and "ens19" in ret[0] and "ens18" in ret[0])
        masq = [c for c in cmds if c[:3] == ["iptables", "-t", "nat"] and "MASQUERADE" in c]
        check("MASQUERADE出外网(wan正确)", len(masq) == 1 and "ens19" in masq[0])
    finally:
        restore()

def test_known_clients():
    print("[known_clients]")
    reset(_tmp(), default="allow", macs={"001122334455": "deny"})
    netctrl._ready = True
    restore = fake_runner([], "10.0.1.9 dev lan0 lladdr 00:99:88:77:66:55 REACHABLE\n")
    try:
        rows = netctrl.known_clients({"aabbccddeeff": "10.0.1.8"})
        macs = {r["mac"] for r in rows}
        check("包含手动+在线+连接", {"001122334455", "009988776655", "aabbccddeeff"} <= macs)
        by = {r["mac"]: r for r in rows}
        check("在线状态", by["009988776655"]["online"] is True and by["001122334455"]["online"] is False)
        check("IP来源", by["aabbccddeeff"]["ip"] == "10.0.1.8")
        check("策略来源", by["001122334455"]["src"] == "手动" and by["aabbccddeeff"]["src"] == "默认")
    finally:
        restore()

def main():
    try:
        test_normalize_mac()
        test_effective()
        test_config_roundtrip()
        test_sync_rules_default_allow()
        test_sync_rules_default_deny()
        test_reject_drop_switch()
        test_set_mac_and_remove()
        test_on_boot_and_reconcile()
        test_is_unicast()
        test_mac_colon()
        test_ignore_multicast_neighbor()
        test_build_base_rules()
        test_known_clients()
        print()
        if _failures:
            print(f"FAILED: {len(_failures)} -> {_failures}")
            sys.exit(1)
        print("ALL PASSED")
    finally:
        shutil.rmtree(_TMP_ROOT, ignore_errors=True)

if __name__ == "__main__":
    main()
