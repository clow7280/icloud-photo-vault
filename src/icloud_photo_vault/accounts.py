"""账号的 Apple ID 整理和改名。"""

import json
import os
import re
import unicodedata
from pathlib import Path

from .config import Config
from .db import Database

PHONE_SEPARATORS = re.compile(r"[\s\-().]")
CN_MOBILE = re.compile(r"(?:\+86|0086|86)?(1[3-9][0-9]{9})")


def normalize_apple_id(apple_id: str, domain: str) -> str:
    """把中国大陆手机号形式的 Apple ID 整理成 86 加 11 位号码，例如 8613812345678。

    这是实际登录成功过的写法。邮箱和其他写法只去掉首尾空白，原样返回。
    只有区域是中国大陆时才整理，因为其他地区的号码可能同样以 1 开头、长度也是 11 位。
    """
    value = apple_id.strip()
    if domain != "cn" or "@" in value:
        return value
    # 全角的数字和加号转成半角，再去掉空格、横线、括号和点
    compact = PHONE_SEPARATORS.sub("", unicodedata.normalize("NFKC", value))
    if m := CN_MOBILE.fullmatch(compact):
        return "86" + m.group(1)
    return value


class RenameError(Exception):
    pass


def rename_account(db: Database, cfg: Config, account_id: int, new_name: str) -> str:
    """改名，并移动这个账号的备份目录和归档包。任何一步失败都撤销已经做过的移动。

    调用方要保证这时没有其他任务在运行。
    """
    with db.connect() as conn:
        account = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        if account is None:
            raise RenameError("账号不存在")
        old_name = account["name"]
        if new_name == old_name:
            return f"名称没有变化，仍是 {old_name}"
        if conn.execute("SELECT 1 FROM accounts WHERE name = ? AND id != ?", (new_name, account_id)).fetchone():
            raise RenameError(f"已经有账号叫 {new_name}")
        archives = conn.execute(
            "SELECT month, volumes FROM archives WHERE account_id = ?", (account_id,)
        ).fetchall()

    for root in (cfg.backup_root, cfg.archive_root):
        if (root / new_name).exists():
            raise RenameError(f"{root / new_name} 已经存在，请换一个名称或先处理这个目录")

    # 先在旧目录里给归档包改名，再整体移动目录
    old_prefix, new_prefix = f"{old_name}-", f"{new_name}-"
    new_volumes: dict[str, list[str]] = {}
    moves: list[tuple[Path, Path]] = []
    for row in archives:
        names = []
        for name in json.loads(row["volumes"]):
            parts = name.split("/")
            if parts[0] != old_name or not parts[-1].startswith(old_prefix):
                raise RenameError(f"归档包路径 {name} 不符合预期，没有改名")
            new_file = new_prefix + parts[-1][len(old_prefix):]
            moves.append((cfg.archive_root.joinpath(*parts), cfg.archive_root.joinpath(*parts[:-1], new_file)))
            names.append("/".join([new_name, *parts[1:-1], new_file]))
        new_volumes[row["month"]] = names
    moves = [(src, dst) for src, dst in moves if src.exists()]
    for root in (cfg.archive_root, cfg.backup_root):
        if (root / old_name).exists():
            moves.append((root / old_name, root / new_name))

    done: list[tuple[Path, Path]] = []
    try:
        for src, dst in moves:
            if dst.exists():
                raise RenameError(f"{dst} 已经存在")
            os.rename(src, dst)
            done.append((src, dst))
        with db.connect() as conn:
            conn.execute("UPDATE accounts SET name = ? WHERE id = ?", (new_name, account_id))
            conn.executemany(
                "UPDATE archives SET volumes = ? WHERE account_id = ? AND month = ?",
                [(json.dumps(names), account_id, month) for month, names in new_volumes.items()],
            )
    except BaseException:
        for src, dst in reversed(done):
            os.rename(dst, src)
        raise
    return f"已把 {old_name} 改名为 {new_name}，备份目录和 {len(archives)} 个月的归档包已一起改名"
