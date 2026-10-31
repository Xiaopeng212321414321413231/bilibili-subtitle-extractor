"""安全删除闸门：所有删除都从这里过，挡住「手一抖搞崩电脑」的那一下。

    python tools/safe_delete.py <路径>...              # 预览（默认，不删）
    python tools/safe_delete.py <路径>... --yes        # 执行（进回收站）
    python tools/safe_delete.py --list-protected       # 看保护清单

## 它挡什么

单靠「小心」是不够的 —— 真正的事故都不是手滑，是判断错：

- 路径拼错，`assets/video` 写成 `assets`，整目录没了
- 相对路径 + cwd 不对，`notes/` 解析到了别的地方
- 通配符写宽了，`*.json` 把配置一起带走
- 以为在临时目录，其实指向了真实数据
- `.git` 被顺手删掉，一个月的提交历史归零

所以这里设了六道闸：

1. **保护清单硬拒绝** —— 系统目录、家目录、桌面/文档/下载根、项目根、`.git`，
   命中就退出，没有任何绕过参数
2. **路径必须绝对且已存在** —— 相对路径先解析成绝对路径再判断，`..` 逃逸直接拒
3. **目录深度下限** —— 层级 ≤ 2 的目录（如 `C:\\Users\\xxx`）一律拒绝，那是家
4. **通配符默认禁止** —— 要展开必须显式 `--glob`，且展开后再过一遍上面的检查
5. **默认 dry-run** —— 不加 `--yes` 只打印清单和总量
6. **大批量二次确认** —— 超过 200 个文件或 2GB，要求同时给 `--yes` 和 `--force`

删除一律走回收站（send2trash），不是 `rm`。后悔了能捞回来。

## 审计

每次执行都追加到 `logs/safe-delete.log`：时间、目标、数量、大小、结果。
出事时能查是谁在什么时候删了什么。
"""

from __future__ import annotations

import argparse
import glob
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG = os.path.join(ROOT, "logs", "safe-delete.log")

# 类型一：整棵树都禁止（系统盘的关键目录，内部任何东西都不许删）
FORBIDDEN_TREES = [
    r"C:\Windows", r"C:\Program Files", r"C:\Program Files (x86)",
    r"C:\ProgramData", r"C:\Users\32352\AppData",
]

# 类型二：只禁止「这个路径本身」（可以删它里面的具体文件，但不能整个端走）
FORBIDDEN_SELF = [
    r"C:\\", r"C:\Users",
    r"C:\Users\32352",                    # 家目录本身
    r"C:\Users\32352\Desktop",
    r"C:\Users\32352\Documents",
    r"C:\Users\32352\Downloads",
    r"C:\Users\32352\Pictures",
    r"C:\Users\32352\Music",
    r"C:\Users\32352\Videos",
    ROOT,                                 # 项目根
    os.path.join(ROOT, ".git"),           # 版本历史
    os.path.join(ROOT, "models"),         # 模型重下代价大
]


def _norm(p: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(p)))


_TREES = [_norm(p) for p in FORBIDDEN_TREES]
_SELVES = [_norm(p) for p in FORBIDDEN_SELF]


def _size_of(p: str) -> int:
    if os.path.isfile(p):
        try:
            return os.path.getsize(p)
        except OSError:
            return 0
    tot = 0
    for root, _, fs in os.walk(p):
        for f in fs:
            try:
                tot += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return tot


def _count_of(p: str) -> int:
    if os.path.isfile(p):
        return 1
    n = 0
    for _, _, fs in os.walk(p):
        n += len(fs)
    return n


def check(p: str) -> str:
    """返回拒绝理由；空字符串表示可以删。"""
    if not os.path.exists(p):
        return "路径不存在"
    a = _norm(p)

    # ① 在系统关键树里（含自身）—— 一律拒
    for t in _TREES:
        if a == t or a.startswith(t + os.sep):
            return "位于受保护的系统目录内"

    # ② 等于某个敏感路径本身 —— 拒
    for s in _SELVES:
        if a == s:
            return "命中保护清单（系统/家/项目关键目录本身）"

    # ③ 是某个敏感路径的祖先 —— 删了会连带端走
    for s in _SELVES:
        if s.startswith(a + os.sep):
            return "该目录是受保护路径的上级"

    # ④ 目录层级过浅
    parts = [x for x in a.split(os.sep) if x]
    if os.path.isdir(p) and len(parts) <= 2:
        return "目录层级过浅（%d 层），像是盘符根或家目录" % len(parts)

    # ⑤ .. 逃逸
    if os.sep + ".." + os.sep in a:
        return "路径含 .. 逃逸"

    return ""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="安全删除（默认只预览）")
    ap.add_argument("paths", nargs="*", help="要删的路径")
    ap.add_argument("--yes", action="store_true", help="真的执行")
    ap.add_argument("--force", action="store_true", help="大批量时需要的额外确认")
    ap.add_argument("--glob", action="store_true", help="允许通配符展开（展开后仍逐个检查）")
    ap.add_argument("--list-protected", action="store_true", help="打印保护清单")
    a = ap.parse_args(argv)

    if a.list_protected:
        print("整棵树禁止（内部任何东西都不许删）：")
        for x in FORBIDDEN_TREES:
            print("  " + x)
        print("\n只禁止自身（里面的具体文件仍可删）：")
        for x in FORBIDDEN_SELF:
            print("  " + x)
        return 0

    if not a.paths:
        ap.print_help()
        return 2

    # 展开通配符（需显式允许）
    targets: list[str] = []
    for raw in a.paths:
        if any(ch in raw for ch in "*?["):
            if not a.glob:
                print("✗ 拒绝：含通配符 %r。确认无误请加 --glob" % raw)
                return 2
            hits = glob.glob(raw, recursive=True)
            print("  通配符 %r 展开出 %d 项" % (raw, len(hits)))
            targets += hits
        else:
            targets.append(raw)

    ok, rejected = [], []
    for p in targets:
        why = check(p)
        if why:
            rejected.append((p, why))
        else:
            ok.append(p)

    total_b = sum(_size_of(p) for p in ok)
    total_n = sum(_count_of(p) for p in ok)

    print()
    print("待删 %d 项 / %d 个文件 / %.1f MB" % (len(ok), total_n, total_b / 1048576))
    for p in ok[:20]:
        print("   - %s" % p)
    if len(ok) > 20:
        print("   …… 另有 %d 项" % (len(ok) - 20))

    if rejected:
        print("\n拒绝 %d 项：" % len(rejected))
        for p, why in rejected[:10]:
            print("   ✗ %s —— %s" % (p, why))
        if len(rejected) > 10:
            print("   …… 另有 %d 项" % (len(rejected) - 10))

    if not ok:
        print("\n没有可删的东西。")
        return 1 if rejected else 0

    # 大批量二次确认
    big = total_n > 200 or total_b > 2 * 1024 ** 3
    if big and not a.force:
        print("\n⚠️  这是一次大批量删除（%d 个文件 / %.1f MB）。"
              % (total_n, total_b / 1048576))
        print("   确认无误请同时加 --yes 和 --force。")
        return 3

    if not a.yes:
        print("\n这是预览。确认后加 --yes 执行（进回收站，可恢复）。")
        return 0

    try:
        from send2trash import send2trash
    except ImportError:
        print("✗ 缺 send2trash。装：pip install send2trash")
        return 2

    done = 0
    for p in ok:
        try:
            send2trash(p)
            done += 1
        except Exception as e:  # noqa: BLE001
            print("   ! %s：%s" % (p, str(e)[:100]))

    _audit(ok, done, total_b)
    print("\n已移入回收站：%d / %d 项（%.1f MB）" % (done, len(ok), total_b / 1048576))
    print("回收站仍占空间，确认无误后清空回收站才真正释放。")
    return 0 if done == len(ok) else 1


def _audit(paths: list[str], done: int, size: int) -> None:
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write("%s\tdone=%d/%d\t%.1fMB\t%s\n" % (
                time.strftime("%Y-%m-%d %H:%M:%S"), done, len(paths),
                size / 1048576, "; ".join(paths[:5]) + ("..." if len(paths) > 5 else "")))
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    raise SystemExit(main())
