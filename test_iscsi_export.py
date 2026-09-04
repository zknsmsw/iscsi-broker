#!/usr/bin/env python3
"""iscsi_broker.py 后台手动 iSCSI 挂载（母盘直出，写直达母盘）功能测试。

纯逻辑测试：不依赖 root / tgtadm / qemu，Windows 上可运行。
覆盖：挂载成功、各种“镜像被占用”拒绝、路径穿越拒绝、tgt 建盘失败回滚、
卸载成功/未知、tid 分配错开区间等正常与异常路径。
"""
import os, sys, shutil
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import iscsi_broker

_failures = []
_TMP_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "zz_export_test")
_SAVED_IMAGES_DIR = iscsi_broker.IMAGES_DIR
_seq = [0]

def _tmp():
    # 注意：目录名不能以 tmp 开头（仓库 .gitignore 忽略 tmp*/，会连累沙箱/清理逻辑）
    os.makedirs(_TMP_ROOT, exist_ok=True)
    _seq[0] += 1
    d = os.path.join(_TMP_ROOT, "d%d" % _seq[0])
    os.makedirs(d, exist_ok=True)
    return d

def check(name, cond):
    if cond:
        print(f"  ok  {name}")
    else:
        _failures.append(name)
        print(f"FAIL  {name}")

class FakeTgt:
    """替换 iscsi_broker 的 tgt_* 函数：记录调用，并可按阶段注入失败。"""
    def __init__(self):
        self.created = []   # (tid, iqn)
        self.luns = []      # (tid, backing, bsoflags)
        self.bound = []     # tid
        self.deleted = []   # tid
        self.fail_on = None # None | 'target' | 'lun' | 'bind'
        self._orig = None
    def new_target(self, tid, iqn):
        if self.fail_on == 'target':
            raise RuntimeError("fake tgt_new_target error")
        self.created.append((tid, iqn))
    def new_lun(self, tid, backing, bsoflags=None):
        if self.fail_on == 'lun':
            raise RuntimeError("fake tgt_new_lun error")
        self.luns.append((tid, backing, bsoflags))
    def bind(self, tid):
        if self.fail_on == 'bind':
            raise RuntimeError("fake tgt_bind_target error")
        self.bound.append(tid)
    def force_delete(self, tid):
        self.deleted.append(tid)
    def install(self):
        self._orig = (iscsi_broker.tgt_new_target, iscsi_broker.tgt_new_lun,
                      iscsi_broker.tgt_bind_target, iscsi_broker._tgt_force_delete)
        iscsi_broker.tgt_new_target = self.new_target
        iscsi_broker.tgt_new_lun = self.new_lun
        iscsi_broker.tgt_bind_target = self.bind
        iscsi_broker._tgt_force_delete = self.force_delete
    def restore(self):
        (iscsi_broker.tgt_new_target, iscsi_broker.tgt_new_lun,
         iscsi_broker.tgt_bind_target, iscsi_broker._tgt_force_delete) = self._orig

def make_image(tmp, name, size=1 << 20):
    p = os.path.join(tmp, name + ".raw")
    with open(p, "wb") as f:
        f.write(b"\0" * size)
    return p

def reset_state(tmp=None):
    iscsi_broker.web_exported.clear()
    iscsi_broker.mac_state.clear()
    iscsi_broker.writeback_active.clear()
    if tmp is not None:
        iscsi_broker.IMAGES_DIR = tmp

def expected_iqn(img):
    return f"iqn.2026-07.storage:web-{img}"

def test_mount_success():
    print("[挂载成功：空闲镜像直出]")
    tmp = _tmp()
    reset_state(tmp)
    fake = FakeTgt(); fake.install()
    try:
        make_image(tmp, "win11")
        ok, iqn = iscsi_broker.export_image_mount("win11")
        check("返回成功", ok is True)
        check("IQN 格式", iqn == expected_iqn("win11"))
        rec = iscsi_broker.web_exported.get("win11")
        check("已登记占用", rec is not None and rec["iqn"] == iqn)
        check("tid 落在手动区间", rec is not None
              and iscsi_broker.WEB_EXPORT_TID_BASE <= rec["tid"]
              < iscsi_broker.WEB_EXPORT_TID_BASE + iscsi_broker.WEB_EXPORT_TID_SLOTS)
        check("建 target 带正确 iqn", fake.created == [(rec["tid"], iqn)])
        check("LUN 直接指向母盘文件", fake.luns == [(rec["tid"], os.path.join(tmp, "win11.raw"), None)])
        check("已 bind", fake.bound == [rec["tid"]])
        reason = iscsi_broker.image_busy_reason("win11")
        check("挂载后 busy 原因含后台挂载", reason is not None and "后台手动挂载" in reason)
    finally:
        fake.restore(); reset_state()
        shutil.rmtree(tmp, ignore_errors=True)

def test_mount_image_with_dash_name():
    print("[镜像名含短横线]")
    tmp = _tmp()
    reset_state(tmp)
    fake = FakeTgt(); fake.install()
    try:
        make_image(tmp, "win-11")
        ok, iqn = iscsi_broker.export_image_mount("win-11")
        check("成功且 iqn 含镜像名", ok is True and iqn == expected_iqn("win-11"))
    finally:
        fake.restore(); reset_state()
        shutil.rmtree(tmp, ignore_errors=True)

def test_mount_rejects_writeback_busy():
    print("[回写占用拒绝挂载]")
    tmp = _tmp()
    reset_state(tmp)
    fake = FakeTgt(); fake.install()
    try:
        make_image(tmp, "win11")
        iscsi_broker.writeback_active["win11"] = "001122334455"
        ok, msg = iscsi_broker.export_image_mount("win11")
        check("拒绝", ok is False and "回写" in msg)
        check("未登记", not iscsi_broker.web_exported)
        check("未调用 tgt", not fake.created and not fake.luns and not fake.bound)
    finally:
        fake.restore(); reset_state()
        shutil.rmtree(tmp, ignore_errors=True)

def test_mount_rejects_overlay_busy():
    print("[叠加占用拒绝挂载]")
    tmp = _tmp()
    reset_state(tmp)
    fake = FakeTgt(); fake.install()
    try:
        make_image(tmp, "win11")
        iscsi_broker.mac_state["001122334455"] = {
            "nbd_dev": None,
            "overlay": os.path.join("X:\\overlay", "overlay_001122334455_win11.raw")}
        ok, msg = iscsi_broker.export_image_mount("win11")
        check("拒绝", ok is False and "叠加模式" in msg)
        check("未登记", not iscsi_broker.web_exported)
        check("未调用 tgt", not fake.created)
    finally:
        fake.restore(); reset_state()
        shutil.rmtree(tmp, ignore_errors=True)

def test_mount_rejects_double():
    print("[重复挂载拒绝]")
    tmp = _tmp()
    reset_state(tmp)
    fake = FakeTgt(); fake.install()
    try:
        make_image(tmp, "win11")
        ok1, _ = iscsi_broker.export_image_mount("win11")
        ok2, msg2 = iscsi_broker.export_image_mount("win11")
        check("第一次成功", ok1 is True)
        check("第二次拒绝且提示先卸载", ok2 is False and "后台手动挂载" in msg2)
        check("tgt 只建过一次", len(fake.created) == 1)
    finally:
        fake.restore(); reset_state()
        shutil.rmtree(tmp, ignore_errors=True)

def test_mount_nonexistent():
    print("[镜像不存在 / 路径穿越拒绝]")
    tmp = _tmp()
    reset_state(tmp)
    fake = FakeTgt(); fake.install()
    try:
        ok, msg = iscsi_broker.export_image_mount("ghost")
        check("不存在拒绝", ok is False and "镜像不存在" in msg)
        ok2, msg2 = iscsi_broker.export_image_mount("..\\..\\evil")
        check("路径穿越拒绝", ok2 is False and "镜像不存在" in msg2)
        check("全程未建盘", not fake.created and not iscsi_broker.web_exported)
    finally:
        fake.restore(); reset_state()
        shutil.rmtree(tmp, ignore_errors=True)

def test_mount_rollback_on_failure():
    print("[tgt 建盘失败回滚登记并清理 target]")
    tmp = _tmp()
    reset_state(tmp)
    fake = FakeTgt(); fake.install()
    try:
        make_image(tmp, "win11")
        fake.fail_on = 'lun'
        ok, msg = iscsi_broker.export_image_mount("win11")
        check("返回失败", ok is False and "挂载失败" in msg)
        check("登记已回滚", not iscsi_broker.web_exported)
        check("清理了已建 target", len(fake.deleted) == 1
              and fake.deleted[0] >= iscsi_broker.WEB_EXPORT_TID_BASE)
    finally:
        fake.restore(); reset_state()
        shutil.rmtree(tmp, ignore_errors=True)

def test_mount_failure_at_target_stage():
    print("[建 target 即失败]")
    tmp = _tmp()
    reset_state(tmp)
    fake = FakeTgt(); fake.install()
    try:
        make_image(tmp, "win11")
        fake.fail_on = 'target'
        ok, msg = iscsi_broker.export_image_mount("win11")
        check("返回失败", ok is False and "挂载失败" in msg)
        check("未登记", not iscsi_broker.web_exported)
    finally:
        fake.restore(); reset_state()
        shutil.rmtree(tmp, ignore_errors=True)

def test_unmount():
    print("[卸载成功 / 未挂载卸载]")
    tmp = _tmp()
    reset_state(tmp)
    fake = FakeTgt(); fake.install()
    try:
        make_image(tmp, "win11")
        ok, _ = iscsi_broker.export_image_mount("win11")
        tid = iscsi_broker.web_exported["win11"]["tid"]
        ok2, msg2 = iscsi_broker.export_image_unmount("win11")
        check("卸载成功", ok2 is True and "已卸载" in msg2)
        check("占用已释放", "win11" not in iscsi_broker.web_exported)
        check("删除的是对应 tid 的 target", tid in fake.deleted)
        ok3, msg3 = iscsi_broker.export_image_unmount("win11")
        check("重复卸载拒绝", ok3 is False and "未处于后台挂载状态" in msg3)
        reason = iscsi_broker.image_busy_reason("win11")
        check("卸载后镜像回到空闲", reason is None)
    finally:
        fake.restore(); reset_state()
        shutil.rmtree(tmp, ignore_errors=True)

def test_tid_allocation_disjoint_and_reuse():
    print("[tid 分配：互不相同、落在手动区间、卸载后可复用]")
    tmp = _tmp()
    reset_state(tmp)
    fake = FakeTgt(); fake.install()
    try:
        make_image(tmp, "win11")
        make_image(tmp, "data02")
        iscsi_broker.export_image_mount("win11")
        iscsi_broker.export_image_mount("data02")
        t1 = iscsi_broker.web_exported["win11"]["tid"]
        t2 = iscsi_broker.web_exported["data02"]["tid"]
        lo, hi = iscsi_broker.WEB_EXPORT_TID_BASE, iscsi_broker.WEB_EXPORT_TID_BASE + iscsi_broker.WEB_EXPORT_TID_SLOTS
        check("两个 tid 都落在手动区间", lo <= t1 < hi and lo <= t2 < hi)
        check("tid 互不相同", t1 != t2)
        check("与 PXE 客户机 tid 区间错开", t1 >= 1_100_000 and t2 >= 1_100_000)
        iscsi_broker.export_image_unmount("win11")
        iscsi_broker.export_image_mount("win11")
        t3 = iscsi_broker.web_exported["win11"]["tid"]
        check("卸载后 tid 可复用", t3 == t1)
        check("仍与另一张盘不冲突", t3 != t2)
    finally:
        fake.restore(); reset_state()
        shutil.rmtree(tmp, ignore_errors=True)

def test_busy_reason_free_image():
    print("[空闲镜像 busy 判定]")
    tmp = _tmp()
    reset_state(tmp)
    try:
        make_image(tmp, "free1")
        check("空闲返回 None", iscsi_broker.image_busy_reason("free1") is None)
        iscsi_broker.writeback_active["free1"] = "001122334455"
        r = iscsi_broker.image_busy_reason("free1")
        check("回写占用原因", r is not None and "回写模式" in r)
    finally:
        reset_state()
        shutil.rmtree(tmp, ignore_errors=True)

def main():
    try:
        test_mount_success()
        test_mount_image_with_dash_name()
        test_mount_rejects_writeback_busy()
        test_mount_rejects_overlay_busy()
        test_mount_rejects_double()
        test_mount_nonexistent()
        test_mount_rollback_on_failure()
        test_mount_failure_at_target_stage()
        test_unmount()
        test_tid_allocation_disjoint_and_reuse()
        test_busy_reason_free_image()
        print()
        if _failures:
            print(f"FAILED: {len(_failures)} -> {_failures}")
            sys.exit(1)
        print("ALL PASSED")
    finally:
        iscsi_broker.IMAGES_DIR = _SAVED_IMAGES_DIR
        shutil.rmtree(_TMP_ROOT, ignore_errors=True)
if __name__ == "__main__":
    main()
