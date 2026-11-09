# VLA 训练排队平台常用命令。底层都调用 scripts/ 下的脚本，不重复实现逻辑。
#
#   make help              列出所有目标
#   make test              前后端单元测试
#   make verify            集群侧验收（只读，不启动训练）
#   make build             构建 Web 镜像并导入 containerd（需要 root）
#   make deploy            部署 Web（PROFILE=4090|a100，默认 4090）
#   make release V=0.5.0   发版：校验 + 测试 + 验收 + 提交 + 打 tag

SHELL := /usr/bin/env bash
ROOT_DIR := $(shell cd -- "$(dir $(lastword $(MAKEFILE_LIST)))" ; pwd)
VERSION := $(shell cat $(ROOT_DIR)/VERSION 2>/dev/null)
PROFILE ?= 4090
V ?=
SUDO ?= sudo
K3S ?= /usr/local/bin/k3s

.PHONY: help test test-backend test-frontend verify build deploy release clean

help:
	@printf 'VLA 训练排队平台 —— 当前版本 %s\n\n' "$$(cat $(ROOT_DIR)/VERSION 2>/dev/null || echo unknown)"
	@printf '  make test              前后端单元测试（不需要集群）\n'
	@printf '  make test-backend      只跑后端 pytest\n'
	@printf '  make test-frontend     只跑前端 vitest\n'
	@printf '  make verify            集群验收 scripts/verify-4090.sh（只读）\n'
	@printf '  make build             构建 Web 镜像并导入 containerd（需 root）\n'
	@printf '  make deploy            PROFILE=4090|a100 部署 Web\n'
	@printf '  make release V=x.y.z   发版（会跑测试与验收，然后提交并打 tag）\n'
	@printf '  make clean             清理 __pycache__、前端构建产物与构建缓存\n'

test:
	@bash $(ROOT_DIR)/scripts/test.sh

test-backend:
	@bash $(ROOT_DIR)/scripts/test.sh backend -q

test-frontend:
	@bash $(ROOT_DIR)/scripts/test.sh frontend

verify:
	@bash $(ROOT_DIR)/scripts/verify-4090.sh

build:
	@cd $(ROOT_DIR)/frontend && \
	  if ! node -e 'process.exit(process.versions.node.split(".")[0] < 18 ? 1 : 0)' 2>/dev/null; then \
	    export PATH=/opt/node20/bin:$$PATH; \
	  fi; \
	  [ -d node_modules ] || npm ci; \
	  npm run build
	@$(SUDO) bash -c 'cd $(ROOT_DIR) && buildah bud --isolation=chroot -t vla-platform:local -f backend/Dockerfile . \
	  && buildah tag localhost/vla-platform:local docker.io/library/vla-platform:local \
	  && buildah push docker.io/library/vla-platform:local docker-archive:/tmp/vla-platform-build.tar >/dev/null \
	  && $(K3S) ctr -n k8s.io images import /tmp/vla-platform-build.tar \
	  && rm -f /tmp/vla-platform-build.tar'

deploy:
	@$(SUDO) PROFILE=$(PROFILE) bash $(ROOT_DIR)/scripts/deploy-web.sh

release:
	@if [ -z "$(V)" ]; then printf '用法：make release V=0.5.0\n' >&2; exit 2; fi
	@bash $(ROOT_DIR)/scripts/release.sh $(V)

clean:
	@find $(ROOT_DIR) -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
	@rm -rf $(ROOT_DIR)/frontend/dist
	@printf '已清理 __pycache__ 与 frontend/dist\n'
