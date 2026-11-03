"""通过 icloudpd 自带的 pyicloud_ipd 访问 iCloud 照片。"""

import json
import logging
import threading
import urllib.parse
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from icloudpd.xmp_sidecar import build_metadata, generate_xml
from pyicloud_ipd.asset_version import ITEM_TYPE_EXTENSIONS
from pyicloud_ipd.base import PyiCloudService
from pyicloud_ipd.exceptions import PyiCloudException
from pyicloud_ipd.item_type import AssetItemType
from pyicloud_ipd.services.photos import PhotoAsset, PhotoLibrary
from pyicloud_ipd.version_size import AssetVersionSize, LivePhotoVersionSize
from requests import RequestException

from .source import LoginRequired, PhotoSource, RemoteAsset, RemoteVersion, SourceError

log = logging.getLogger(__name__)

CHUNK_SIZE = 1 << 20


def _ext(file_type: str, fallback_name: str) -> str:
    if file_type in ITEM_TYPE_EXTENSIONS:
        return ITEM_TYPE_EXTENSIONS[file_type]
    suffix = Path(fallback_name).suffix.lstrip(".")
    return suffix.upper() or "BIN"


def _xmp(photo: PhotoAsset) -> bytes | None:
    try:
        doc = generate_xml(build_metadata(photo._asset_record))
        return ElementTree.tostring(doc, encoding="utf-8", xml_declaration=True)
    except Exception as e:  # XMP 只是附加信息，生成失败不影响备份
        log.warning("无法生成 %s 的 XMP：%s", photo.filename, e)
        return None


def _taken_at(photo: PhotoAsset):
    # 没有拍摄日期时 pyicloud_ipd 会退回 1970 年，这里如实返回 None
    try:
        photo._asset_record["fields"]["assetDate"]["value"]
    except (KeyError, TypeError):
        return None
    return photo.asset_date


def to_remote_asset(photo: PhotoAsset) -> RemoteAsset:
    versions = photo.versions
    original = versions.get(AssetVersionSize.ORIGINAL)
    if original is None:
        raise SourceError(f"{photo.filename} 没有原图版本")
    filename = photo.filename
    remote: dict[str, RemoteVersion] = {
        "original": RemoteVersion(original.url, original.size, original.checksum, _ext(original.type, filename))
    }
    is_image = photo.item_type != AssetItemType.MOVIE
    if is_image:
        lookups = (
            ("alternative", AssetVersionSize.ALTERNATIVE),
            ("adjusted", AssetVersionSize.ADJUSTED),
            ("live", LivePhotoVersionSize.ORIGINAL),
        )
        for role, key in lookups:
            version = versions.get(key)
            # 没有编辑过的照片，编辑版与原图是同一个文件，不重复下载
            if version is not None and version.checksum != original.checksum:
                remote[role] = RemoteVersion(version.url, version.size, version.checksum, _ext(version.type, filename))
    return RemoteAsset(
        asset_id=photo.id,
        record_name=photo._asset_record["recordName"],
        change_tag=photo._asset_record.get("recordChangeTag"),
        filename=filename,
        item_type="image" if is_image else "movie",
        taken_at=_taken_at(photo),
        versions=remote,
        xmp=_xmp(photo),
    )


class ICloudSource:
    def __init__(self, service: PyiCloudService):
        self.service = service
        self.library: PhotoLibrary = service.photos
        # 无法解析的照片记在这里，同步时不会把它们误判为已从 iCloud 删除
        self.unreadable: list[str] = []

    def iter_assets(self) -> Iterator[RemoteAsset]:
        self.unreadable = []
        for photo in self.library.all:
            try:
                yield to_remote_asset(photo)
            except (SourceError, KeyError, ValueError) as e:
                log.warning("跳过无法解析的照片 %s：%s", photo.id, e)
                self.unreadable.append(photo.id)

    def download(self, asset: RemoteAsset, role: str) -> Iterator[bytes]:
        version = asset.versions[role]
        try:
            response = self.library.session.get(version.url, stream=True)
        except (PyiCloudException, RequestException) as e:
            raise SourceError(f"下载 {asset.filename} 失败：{e}") from e
        if not response.ok:
            raise SourceError(f"下载 {asset.filename} 失败：HTTP {response.status_code}")
        yield from response.iter_content(chunk_size=CHUNK_SIZE)

    def delete(self, record_name: str, change_tag: str | None) -> None:
        """与 icloudpd 的 Move 模式相同：把记录标记为已删除，照片进入“最近删除”。"""
        record: dict[str, Any] = {
            "fields": {"isDeleted": {"value": 1}},
            "recordName": record_name,
            "recordType": "CPLAsset",
        }
        if change_tag:
            record["recordChangeTag"] = change_tag
        body = {
            "atomic": True,
            "desiredKeys": ["isDeleted"],
            "operations": [{"operationType": "update", "record": record}],
            "zoneID": self.library.zone_id,
        }
        url = f"{self.library.service_endpoint}/records/modify?{urllib.parse.urlencode(self.library.params)}"
        try:
            response = self.library.session.post(
                url, data=json.dumps(body), headers={"Content-type": "application/json"}
            )
            data = response.json()
        except (PyiCloudException, RequestException, ValueError) as e:
            raise SourceError(f"删除 {record_name} 失败：{e}") from e
        records = data.get("records") or []
        if not records:
            raise SourceError(f"删除 {record_name} 失败：iCloud 没有返回记录")
        error = records[0].get("serverErrorCode")
        if error:
            raise SourceError(f"删除 {record_name} 失败：{error} {records[0].get('reason', '')}".strip())


class ICloudBackend:
    def __init__(self, sessions_dir: Path):
        self.sessions_dir = sessions_dir
        self._pending: dict[int, PyiCloudService] = {}
        self._lock = threading.Lock()

    def _cookie_dir(self, account_id: int) -> str:
        return str(self.sessions_dir / str(account_id))

    def start_login(self, account_id: int, domain: str, apple_id: str, password: str) -> str:
        if not password:
            raise SourceError("密码不能为空")
        try:
            service = PyiCloudService(
                domain, apple_id, lambda: password, cookie_directory=self._cookie_dir(account_id)
            )
        except PyiCloudException as e:
            raise SourceError(f"登录失败：{e}") from e
        if service.requires_2sa and not service.requires_2fa:
            raise SourceError("这个账号使用旧版两步验证，暂不支持")
        if service.requires_2fa:
            if not service.trigger_push_notification():
                log.info("没能主动推送验证码，用户仍可以输入从其他途径收到的验证码")
            with self._lock:
                self._pending[account_id] = service
            return "need_code"
        return "ok"

    def submit_code(self, account_id: int, code: str) -> bool:
        with self._lock:
            service = self._pending.get(account_id)
        if service is None:
            raise SourceError("请先输入密码")
        try:
            ok = service.validate_2fa_code(code.strip())
        except PyiCloudException as e:
            raise SourceError(f"验证失败：{e}") from e
        if ok:
            with self._lock:
                self._pending.pop(account_id, None)
        return ok

    def open_source(self, account_id: int, domain: str, apple_id: str) -> PhotoSource:
        try:
            service = PyiCloudService(
                domain, apple_id, lambda: None, cookie_directory=self._cookie_dir(account_id)
            )
        except PyiCloudException as e:
            raise LoginRequired(f"需要重新登录：{e}") from e
        if service.requires_2fa or service.requires_2sa:
            raise LoginRequired("需要重新做双重认证")
        try:
            return ICloudSource(service)
        except PyiCloudException as e:
            raise SourceError(f"无法打开照片库：{e}") from e
