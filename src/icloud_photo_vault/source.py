"""照片来源的统一接口，以及开发和测试用的模拟来源。"""

import hashlib
import json
import random
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

# 一张照片可能包含的文件：原图、RAW+JPEG 中的另一份、编辑版、Live Photo 视频
REMOTE_ROLES = ("original", "alternative", "adjusted", "live")


class SourceError(Exception):
    pass


class LoginRequired(SourceError):
    pass


@dataclass(frozen=True)
class RemoteVersion:
    url: str
    size: int
    checksum: str
    ext: str


@dataclass
class RemoteAsset:
    asset_id: str
    record_name: str
    change_tag: str | None
    filename: str
    item_type: str
    taken_at: datetime | None
    versions: dict[str, RemoteVersion]
    xmp: bytes | None = None


class PhotoSource(Protocol):
    def iter_assets(self) -> Iterator[RemoteAsset]: ...

    def download(self, asset: RemoteAsset, role: str) -> Iterator[bytes]: ...

    def delete(self, record_name: str, change_tag: str | None) -> None: ...


class Backend(Protocol):
    """负责登录和打开照片来源。"""

    def start_login(self, account_id: int, domain: str, apple_id: str, password: str) -> str: ...

    def submit_code(self, account_id: int, code: str) -> bool: ...

    def open_source(self, account_id: int, domain: str, apple_id: str) -> PhotoSource: ...


# ---- 模拟来源 ----

FAKE_CODE = "123456"


def _fake_bytes(seed: str, size: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < size:
        out += hashlib.sha256(f"{seed}:{counter}".encode()).digest()
        counter += 1
    return bytes(out[:size])


@dataclass
class FakeLibrary:
    """确定性生成的模拟照片库，删除记录保存在 JSON 文件中。"""

    assets: list[RemoteAsset]
    contents: dict[tuple[str, str], bytes]
    state_file: Path | None = None
    deleted: set[str] = field(default_factory=set)
    logged_in: bool = False
    fail_delete: set[str] = field(default_factory=set)

    @classmethod
    def generate(
        cls, seed: str, count: int = 120, now: datetime | None = None, state_file: Path | None = None
    ) -> "FakeLibrary":
        rng = random.Random(seed)
        now = now or datetime.now(UTC)
        assets: list[RemoteAsset] = []
        contents: dict[tuple[str, str], bytes] = {}
        for i in range(count):
            asset_id = f"{seed}-asset-{i:04d}"
            taken_at = None if i == 7 else now - timedelta(days=rng.randint(1, 365 * 6))
            is_movie = rng.random() < 0.15
            stem = f"IMG_{rng.randint(1, 9999):04d}"
            versions: dict[str, RemoteVersion] = {}

            def add(role: str, ext: str) -> None:
                size = rng.randint(2_000, 40_000)
                data = _fake_bytes(f"{asset_id}:{role}", size)
                contents[(asset_id, role)] = data
                checksum = hashlib.sha1(data).hexdigest()
                versions[role] = RemoteVersion(f"fake://{asset_id}/{role}", size, checksum, ext)

            if is_movie:
                filename = f"{stem}.MOV"
                add("original", "MOV")
            else:
                filename = f"{stem}.HEIC"
                add("original", "HEIC")
                if rng.random() < 0.3:
                    add("live", "MOV")
                if rng.random() < 0.15:
                    add("adjusted", "JPG")
            xmp = f'<?xml version="1.0"?><x:xmpmeta id="{asset_id}"/>'.encode()
            assets.append(
                RemoteAsset(
                    asset_id=asset_id,
                    record_name=f"rec-{asset_id}",
                    change_tag="t1",
                    filename=filename,
                    item_type="movie" if is_movie else "image",
                    taken_at=taken_at,
                    versions=versions,
                    xmp=xmp,
                )
            )
        lib = cls(assets, contents, state_file)
        lib._load()
        return lib

    def _load(self) -> None:
        if self.state_file and self.state_file.exists():
            data = json.loads(self.state_file.read_text())
            self.deleted = set(data.get("deleted", []))
            self.logged_in = bool(data.get("logged_in", False))

    def _save(self) -> None:
        if self.state_file:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            data = {"deleted": sorted(self.deleted), "logged_in": self.logged_in}
            self.state_file.write_text(json.dumps(data))

    def iter_assets(self) -> Iterator[RemoteAsset]:
        for asset in self.assets:
            if asset.record_name not in self.deleted:
                yield asset

    def download(self, asset: RemoteAsset, role: str) -> Iterator[bytes]:
        data = self.contents[(asset.asset_id, role)]
        for start in range(0, len(data), 4096):
            yield data[start : start + 4096]

    def delete(self, record_name: str, change_tag: str | None) -> None:
        if record_name in self.fail_delete:
            raise SourceError(f"模拟删除失败：{record_name}")
        self.deleted.add(record_name)
        self._save()


class FakeBackend:
    """开发用的模拟后端：任意密码都进入验证码步骤，验证码固定为 123456。"""

    def __init__(self, state_dir: Path, count: int = 120):
        self.state_dir = state_dir
        self.count = count
        self._libraries: dict[int, FakeLibrary] = {}
        self._pending: set[int] = set()

    def library(self, account_id: int, apple_id: str) -> FakeLibrary:
        if account_id not in self._libraries:
            self._libraries[account_id] = FakeLibrary.generate(
                apple_id, self.count, state_file=self.state_dir / "fake" / f"{account_id}.json"
            )
        return self._libraries[account_id]

    def start_login(self, account_id: int, domain: str, apple_id: str, password: str) -> str:
        if not password:
            raise SourceError("密码不能为空")
        self.library(account_id, apple_id)
        self._pending.add(account_id)
        return "need_code"

    def submit_code(self, account_id: int, code: str) -> bool:
        if account_id not in self._pending:
            raise SourceError("请先输入密码")
        if code != FAKE_CODE:
            return False
        self._pending.discard(account_id)
        lib = self._libraries[account_id]
        lib.logged_in = True
        lib._save()
        return True

    def open_source(self, account_id: int, domain: str, apple_id: str) -> PhotoSource:
        lib = self.library(account_id, apple_id)
        if not lib.logged_in:
            raise LoginRequired("模拟账号尚未登录")
        return lib
