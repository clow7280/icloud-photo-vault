import base64
import json

import pytest
from pyicloud_ipd.services.photos import PhotoAsset

from icloud_photo_vault.icloud import ICloudSource, to_remote_asset
from icloud_photo_vault.source import SourceError


def res(size, url, checksum):
    return {"value": {"size": size, "downloadURL": url, "fileChecksum": checksum}}


def make_photo(item_type="public.heic", extra_master=None, extra_asset=None, asset_date=1683873015000):
    master = {
        "recordName": "MASTER1",
        "fields": {
            "filenameEnc": {"value": base64.b64encode("IMG_0001.HEIC".encode()).decode(), "type": "ENCRYPTED_BYTES"},
            "itemType": {"value": item_type},
            "resOriginalRes": res(1000, "https://example/orig", "c-orig"),
            "resOriginalFileType": {"value": item_type},
            "resOriginalWidth": {"value": 10},
            "resOriginalHeight": {"value": 10},
            **(extra_master or {}),
        },
    }
    asset = {
        "recordName": "ASSET1",
        "recordChangeTag": "tag7",
        "fields": {**({"assetDate": {"value": asset_date}} if asset_date else {}), **(extra_asset or {})},
    }
    return PhotoAsset(master, asset)


def test_converts_original_live_and_edit():
    photo = make_photo(
        extra_master={
            "resOriginalVidComplRes": res(500, "https://example/live", "c-live"),
            "resOriginalVidComplFileType": {"value": "com.apple.quicktime-movie"},
        },
        extra_asset={
            "resJPEGFullRes": res(800, "https://example/edit", "c-edit"),
            "resJPEGFullFileType": {"value": "public.jpeg"},
            "isFavorite": {"value": 1},
        },
    )

    asset = to_remote_asset(photo)

    assert (asset.asset_id, asset.record_name, asset.change_tag) == ("MASTER1", "ASSET1", "tag7")
    assert asset.filename == "IMG_0001.HEIC"
    assert asset.item_type == "image"
    assert asset.taken_at.isoformat() == "2023-05-12T06:30:15+00:00"
    assert {role: (v.size, v.ext) for role, v in asset.versions.items()} == {
        "original": (1000, "HEIC"),
        "live": (500, "MOV"),
        "adjusted": (800, "JPG"),
    }
    assert b"xmp:Rating" in asset.xmp or b"Rating" in asset.xmp


def test_unedited_photo_has_no_adjusted_copy():
    photo = make_photo(
        extra_asset={"resJPEGFullRes": res(1000, "https://example/orig", "c-orig"), "resJPEGFullFileType": {"value": "public.heic"}}
    )
    assert set(to_remote_asset(photo).versions) == {"original"}


def test_video_has_only_original():
    photo = make_photo(item_type="com.apple.quicktime-movie")
    asset = to_remote_asset(photo)
    assert asset.item_type == "movie" and set(asset.versions) == {"original"}


def test_missing_date_is_none():
    assert to_remote_asset(make_photo(asset_date=None)).taken_at is None


class FakeResponse:
    def __init__(self, data):
        self.data = data

    def json(self):
        return self.data


class FakeSession:
    def __init__(self, data):
        self.data = data
        self.posts = []

    def post(self, url, data, headers):
        self.posts.append((url, json.loads(data)))
        return FakeResponse(self.data)


class FakeLibrary:
    service_endpoint = "https://ck/database/1/com.apple.photos.cloud/production/private"
    params = {"dsid": "1"}
    zone_id = {"zoneName": "PrimarySync"}

    def __init__(self, session):
        self.session = session


def make_source(response):
    source = ICloudSource.__new__(ICloudSource)
    source.library = FakeLibrary(FakeSession(response))
    return source


def test_delete_marks_record_deleted():
    source = make_source({"records": [{"recordName": "ASSET1", "fields": {"isDeleted": {"value": 1}}}]})

    source.delete("ASSET1", "tag7")

    url, body = source.library.session.posts[0]
    assert url.startswith(FakeLibrary.service_endpoint + "/records/modify?")
    assert body["zoneID"] == {"zoneName": "PrimarySync"}
    record = body["operations"][0]["record"]
    assert record == {
        "fields": {"isDeleted": {"value": 1}},
        "recordName": "ASSET1",
        "recordType": "CPLAsset",
        "recordChangeTag": "tag7",
    }


def test_delete_reports_server_error():
    source = make_source({"records": [{"recordName": "ASSET1", "serverErrorCode": "CONFLICT", "reason": "oplock"}]})
    with pytest.raises(SourceError, match="CONFLICT"):
        source.delete("ASSET1", "old-tag")


class Sized:
    def __init__(self, n):
        self.n = n

    def __len__(self):
        return self.n


def test_count_uses_index_lookup():
    source = ICloudSource.__new__(ICloudSource)
    source.library = type("Lib", (), {"all": Sized(3200), "albums": {"Videos": Sized(250)}})()
    count = source.count()
    assert (count.total, count.videos) == (3200, 250)


def test_count_failure_is_source_error():
    source = ICloudSource.__new__(ICloudSource)
    source.library = type("Lib", (), {"all": Sized(10), "albums": {}})()
    with pytest.raises(SourceError, match="查询照片数量失败"):
        source.count()


class Response:
    def __init__(self, status, body):
        self.status_code = status
        self.ok = status < 400
        self.body = body

    def iter_content(self, chunk_size):
        for i in range(0, len(self.body), 3):
            yield self.body[i : i + 3]


class DownloadSession:
    def __init__(self, result):
        self.result = result
        self.headers = None

    def get(self, url, stream, headers=None):
        self.headers = headers
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def download(result, start=0):
    from icloud_photo_vault.source import RemoteAsset, RemoteVersion

    source = ICloudSource.__new__(ICloudSource)
    source.library = type("Lib", (), {"session": DownloadSession(result)})()
    asset = RemoteAsset("a", "r", None, "IMG.HEIC", "image", None, {"original": RemoteVersion("u", 10, "c", "HEIC")})
    return b"".join(source.download(asset, "original", start)), source.library.session.headers


def test_download_resumes_with_range():
    assert download(Response(206, b"6789"), 6) == (b"6789", {"Range": "bytes=6-"})
    # 服务器忽略 Range、返回整个文件时，跳过已有的部分
    assert download(Response(200, b"0123456789"), 6)[0] == b"6789"
    assert download(Response(200, b"0123"), 0) == (b"0123", None)


@pytest.mark.parametrize(
    ("error", "message", "retryable"),
    [
        ("connection", "连接 iCloud 失败", True),
        ("timeout", "下载超时", True),
        ("gone", "下载链接已过期", False),
        ("busy", "iCloud 返回 HTTP 503", True),
        ("forbidden", "iCloud 返回 HTTP 403", False),
    ],
)
def test_download_errors_are_explained(error, message, retryable):
    from pyicloud_ipd.exceptions import (
        PyiCloudAPIResponseException,
        PyiCloudConnectionErrorException,
    )
    from requests import ReadTimeout

    result = {
        "connection": PyiCloudConnectionErrorException("Cannot connect to Apple iCloud service"),
        "timeout": ReadTimeout("Read timed out."),
        "gone": PyiCloudAPIResponseException("Gone", "410"),
        "busy": Response(503, b""),
        "forbidden": Response(403, b""),
    }[error]
    with pytest.raises(SourceError) as info:
        download(result)
    assert (str(info.value), info.value.retryable) == (message, retryable)
