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
        check("deny规则1条", len(mac_rules) == 1 and mac_rules[0][-1] == "REJECT")
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
    restore = fake_runner(cmds, "10.0.1.5 dev lan0 lladdr 11:22:33:44:55:66 REACHABLE\n")
    try:
        netctrl.on_client_boot("112233445566")
        check("开机记入缓存", "112233445566" in netctrl._recent_boots)
        check("开机触发规则", any("--mac-source" in c for c in cmds))
        cmds.clear()
        netctrl._recent_boots.clear()
        netctrl.reconcile_once(["aabbccddeeff"])
        check("巡检合并连接+邻居表", any("--mac-source" in c and "aabbccddeeff" in c for c in cmds))
    finally:
        restore()

def test_known_clients():
    print("[known_clients]")
    reset(_tmp(), default="allow", macs={"001122334455": "deny"})
    netctrl._ready = True
    restore = fake_runner([], "10.0.1.9 dev lan0 lladdr 99:88:77:66:55:44 REACHABLE\n")
    try:
        rows = netctrl.known_clients({"aabbccddeeff": "10.0.1.8"})
        macs = {r["mac"] for r in rows}
        check("包含手动+在线+连接", {"001122334455", "998877665544", "aabbccddeeff"} <= macs)
        by = {r["mac"]: r for r in rows}
        check("在线状态", by["998877665544"]["online"] is True and by["001122334455"]["online"] is False)
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
