"""命令行入口。"""

import argparse
import logging
import os
import socket
import stat
import sys

from .config import Config


def bind_socket(path: str, uid: int, gid: int) -> socket.socket:
    """先绑定 Unix Socket 并收紧权限，再交给 uvicorn，避免出现任何人都能连接的间隙。"""
    try:
        if stat.S_ISSOCK(os.stat(path).st_mode):
            os.unlink(path)
    except FileNotFoundError:
        pass
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old_umask = os.umask(0o177)
    try:
        sock.bind(path)
    finally:
        os.umask(old_umask)
    os.chown(path, uid, gid)
    os.chmod(path, 0o600)
    return sock


def run_identity() -> tuple[int, int]:
    """以 root 启动时降权到 VAULT_RUN_UID 和 VAULT_RUN_GID 指定的用户。"""
    if os.getuid() != 0:
        return os.getuid(), os.getgid()
    uid, gid = os.environ.get("VAULT_RUN_UID"), os.environ.get("VAULT_RUN_GID")
    if not uid or not gid:
        sys.exit("不能以 root 身份运行，请设置 VAULT_RUN_UID 和 VAULT_RUN_GID")
    return int(uid), int(gid)


def drop_privileges(uid: int, gid: int) -> None:
    if os.getuid() == 0:
        os.setgroups([])
        os.setgid(gid)
        os.setuid(uid)


def main() -> None:
    parser = argparse.ArgumentParser(prog="icloud-photo-vault")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="启动网页界面和定时任务")
    serve.add_argument("--no-scheduler", action="store_true", help="不启动定时任务")
    sub.add_parser("cycle", help="立即完整运行一轮：同步、归档、自动删除")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = Config.from_env()
    uid, gid = run_identity()
    sock = bind_socket(str(cfg.socket), uid, gid) if args.command == "serve" and cfg.socket else None
    drop_privileges(uid, gid)

    import uvicorn

    from .web.app import create_app

    if args.command == "serve":
        app = create_app(cfg, start_scheduler=not args.no_scheduler)
        if sock is not None:
            uvicorn.Server(uvicorn.Config(app, fd=sock.fileno())).run()
        else:
            uvicorn.run(app, host=cfg.host, port=cfg.port)
    elif args.command == "cycle":
        app = create_app(cfg, start_scheduler=False)
        jobs = app.state.jobs
        jobs.submit_cycle("命令行运行")
        jobs.wait()
        print(jobs.history[0].summary)


if __name__ == "__main__":
    main()
