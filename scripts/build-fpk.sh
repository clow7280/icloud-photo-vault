#!/bin/bash
# 构建 x86_64 镜像，连同 fnos/ 下的应用包源文件打成 dist/icloud-photo-vault-<版本>.fpk
set -euo pipefail
cd "$(dirname "$0")/.."

VERSION=$(sed -n 's/^version = "\(.*\)"$/\1/p' pyproject.toml)
FNPACK="${FNPACK:-$PWD/.tools/fnpack}"
IMAGE="icloud-photo-vault:${VERSION}"
BUILD=dist/fnos

[ -x "$FNPACK" ] || { echo "找不到 fnpack：$FNPACK" >&2; exit 1; }

rm -rf "$BUILD"
mkdir -p dist
cp -R fnos "$BUILD"
find "$BUILD" -name .DS_Store -delete
for f in manifest app/docker/docker-compose.yaml cmd/common; do
    perl -pi -e "s/\@VERSION\@/${VERSION}/g" "$BUILD/$f"
done

docker buildx build --platform linux/amd64 --provenance=false --sbom=false -t "$IMAGE" --load .
docker save --platform linux/amd64 "$IMAGE" | gzip -6 > "$BUILD/app/docker/icloud-photo-vault-image.tar.gz"

(cd "$BUILD" && "$FNPACK" build)
mv "$BUILD/icloud-photo-vault.fpk" "dist/icloud-photo-vault-${VERSION}.fpk"
echo "已生成 dist/icloud-photo-vault-${VERSION}.fpk"
