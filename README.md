# icloud-photo-vault

本项目把多个 iCloud 账号的照片备份到 fnOS NAS，定期打包成加密归档，由百度网盘同步作为第二份副本，并从 iCloud 删除已经备份的照片，以释放 iCloud 空间。
iCloud 访问使用 [icloudpd](https://github.com/icloud-photos-downloader/icloud_photos_downloader) 自带的 `pyicloud_ipd` 库。

需求和设计见 [设计方案](docs/design.md)。

## 本地运行

需要 uv 和 7-Zip 的 `7zz` 命令，macOS 上可以用 `brew install sevenzip` 安装。
下面的命令用模拟照片库启动界面，地址是 <http://127.0.0.1:8730>，模拟账号的验证码是 `123456`：

```bash
VAULT_SOURCE=fake uv run icloud-photo-vault serve
```

运行测试：

```bash
uv run pytest
```

## 打包 fnOS 应用

需要 Docker 和 fnpack，fnpack 从 [飞牛开发者文档](https://developer.fnnas.com/docs/cli/fnpack/) 下载后放到 `.tools/fnpack`：

```bash
scripts/build-fpk.sh
```

生成的 `dist/icloud-photo-vault-<版本>.fpk` 在飞牛应用中心里手动安装。
应用包里的备份目录、归档目录和运行用户是按作者的 NAS 写死的，在其他 NAS 上使用前要改 `fnos/app/docker/docker-compose.yaml` 和 `fnos/cmd/common`。

## 配置

| 环境变量            | 默认值             | 说明                                        |
| ------------------- | ------------------ | ------------------------------------------- |
| VAULT_SOURCE        | icloud             | 设为 `fake` 时使用模拟照片库                |
| VAULT_DATA_DIR      | data               | 下面几个目录的默认上级目录                  |
| VAULT_BACKUP_ROOT   | data/backup        | 备份目录                                    |
| VAULT_ARCHIVE_ROOT  | data/archive       | 归档目录，由百度网盘同步                    |
| VAULT_STATE_DIR     | data/state         | 数据库、会话文件和归档密码                  |
| VAULT_STAGING_DIR   | data/state/staging | 生成归档包的暂存目录                        |
| VAULT_TIMEZONE      | Asia/Shanghai      | 按这个时区划分月份                          |
| VAULT_SEVENZIP      | 7zz                | 7-Zip 命令                                  |
| VAULT_HOST          | 127.0.0.1          | 监听地址                                    |
| VAULT_PORT          | 8730               | 监听端口                                    |
| VAULT_SOCKET        |                    | 设置后改为在这个 Unix Socket 上监听         |
| VAULT_BASE_PATH     |                    | 所有页面的路径前缀                          |
| VAULT_REQUIRE_ADMIN | 0                  | 设为 1 时只接受 fnOS 网关标记为管理员的请求 |

## 许可证

本项目以 [MIT 许可证](LICENSE) 发布。
