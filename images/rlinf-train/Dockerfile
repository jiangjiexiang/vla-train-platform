ARG RLINF_BASE_IMAGE=rlinf/rlinf:agentic-rlinf0.4-maniskill_libero
FROM ${RLINF_BASE_IMAGE}

ARG RLINF_TAG=v0.4
ARG RLINF_COMMIT=2348ece3df113874671478be1360ae3115b346e3
ARG RLINF_GIT_URL=https://github.com/RLinf/RLinf.git

USER root

ENV NVIDIA_DRIVER_CAPABILITIES=all \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /workspace/RLinf

RUN set -eux; \
    git clone --depth 1 --branch "${RLINF_TAG}" "${RLINF_GIT_URL}" /tmp/RLinf; \
    test "$(git -C /tmp/RLinf rev-parse HEAD)" = "${RLINF_COMMIT}"; \
    cp -a /tmp/RLinf/. /workspace/RLinf/; \
    rm -rf /tmp/RLinf

RUN /bin/bash -lc '\
    source switch_env openvla && \
    python -c "\
import torch, ray, rlinf; \
assert torch.version.cuda, \"openvla has CPU-only PyTorch\"; \
assert \"/workspace/RLinf/\" in rlinf.__file__, rlinf.__file__; \
print(\"openvla OK\", torch.__version__, torch.version.cuda, ray.__version__, rlinf.__file__)"'

RUN /bin/bash -lc '\
    source switch_env openvla-oft && \
    python -c "\
import torch, ray, rlinf; \
assert torch.version.cuda, \"openvla-oft has CPU-only PyTorch\"; \
assert \"/workspace/RLinf/\" in rlinf.__file__, rlinf.__file__; \
print(\"openvla-oft OK\", torch.__version__, torch.version.cuda, ray.__version__, rlinf.__file__)"'

RUN /bin/bash -lc '\
    source switch_env openpi && \
    python -c "\
import torch, ray, rlinf; \
assert torch.version.cuda, \"openpi has CPU-only PyTorch\"; \
assert \"/workspace/RLinf/\" in rlinf.__file__, rlinf.__file__; \
print(\"openpi OK\", torch.__version__, torch.version.cuda, ray.__version__, rlinf.__file__)"'

COPY run_ppo.sh /usr/local/bin/run_ppo.sh
COPY config/robocasa365_loaddishwasher_ppo_openpi_pi05.yaml \
    /workspace/RLinf/examples/embodiment/config/robocasa365_loaddishwasher_ppo_openpi_pi05.yaml
RUN chmod +x /usr/local/bin/run_ppo.sh

CMD ["/bin/bash"]
