from __future__ import annotations

from typing import Any, Callable, TypeVar

from kubernetes import client, config
from kubernetes.client.rest import ApiException
from kubernetes.config.config_exception import ConfigException

from ..config import Settings
from .errors import ServiceError


T = TypeVar("T")
JOB_GROUP = "kubeflow.org"
JOB_VERSION = "v1"
JOB_PLURAL = "pytorchjobs"
KUEUE_GROUP = "kueue.x-k8s.io"
KUEUE_VERSION = "v1beta2"


class KubernetesClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._custom: client.CustomObjectsApi | None = None
        self._core: client.CoreV1Api | None = None

    def _initialize(self) -> None:
        if self._custom is not None and self._core is not None:
            return
        try:
            if self.settings.kubeconfig:
                config.load_kube_config(config_file=self.settings.kubeconfig)
            else:
                config.load_incluster_config()
            self._custom = client.CustomObjectsApi()
            self._core = client.CoreV1Api()
        except (ConfigException, OSError) as exc:
            raise ServiceError(
                503,
                f"Kubernetes configuration unavailable: {exc}. "
                "Set PLATFORM_KUBECONFIG or run the service in cluster.",
            ) from exc

    def _custom_api(self) -> client.CustomObjectsApi:
        self._initialize()
        return self._custom  # type: ignore[return-value]

    def _core_api(self) -> client.CoreV1Api:
        self._initialize()
        return self._core  # type: ignore[return-value]

    def _call(self, operation: Callable[[], T]) -> T:
        try:
            return operation()
        except ApiException as exc:
            message = _api_exception_message(exc)
            if exc.status == 404:
                raise ServiceError(404, message) from exc
            if exc.status == 409:
                raise ServiceError(409, message) from exc
            if exc.status in (400, 422):
                # The API server rejected the object we built, which means the
                # submitted spec is invalid - not that the cluster is unhealthy.
                raise ServiceError(422, f"Kubernetes rejected the request: {message}") from exc
            status = 503 if exc.status in (401, 403) or exc.status >= 500 else 502
            raise ServiceError(status, f"Kubernetes API error: {message}") from exc
        except (OSError, TimeoutError) as exc:
            raise ServiceError(503, f"Kubernetes API unavailable: {exc}") from exc

    def get_job(self, name: str) -> dict[str, Any] | None:
        try:
            return self._call(
                lambda: self._custom_api().get_namespaced_custom_object(
                    JOB_GROUP, JOB_VERSION, self.settings.namespace, JOB_PLURAL, name
                )
            )
        except ServiceError as exc:
            if exc.status_code == 404:
                return None
            raise

    def list_jobs(self) -> list[dict[str, Any]]:
        response = self._call(
            lambda: self._custom_api().list_namespaced_custom_object(
                JOB_GROUP,
                JOB_VERSION,
                self.settings.namespace,
                JOB_PLURAL,
                label_selector="app.kubernetes.io/name=vla-train-platform",
            )
        )
        return response.get("items", [])

    def create_job(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._call(
            lambda: self._custom_api().create_namespaced_custom_object(
                JOB_GROUP, JOB_VERSION, self.settings.namespace, JOB_PLURAL, body
            )
        )

    def delete_job(self, name: str) -> None:
        self._call(
            lambda: self._custom_api().delete_namespaced_custom_object(
                JOB_GROUP,
                JOB_VERSION,
                self.settings.namespace,
                JOB_PLURAL,
                name,
                body=client.V1DeleteOptions(propagation_policy="Background"),
            )
        )

    def list_workloads(self) -> list[dict[str, Any]]:
        response = self._call(
            lambda: self._custom_api().list_namespaced_custom_object(
                KUEUE_GROUP, KUEUE_VERSION, self.settings.namespace, "workloads"
            )
        )
        return response.get("items", [])

    def list_job_pods(self, name: str) -> list[dict[str, Any]]:
        response = self._call(
            lambda: self._core_api().list_namespaced_pod(
                self.settings.namespace,
                label_selector=f"vla-platform/job-name={name}",
            )
        )
        return [item.to_dict() for item in response.items]

    def read_pod_log(self, pod_name: str, tail_lines: int) -> str:
        try:
            return self._call(
                lambda: self._core_api().read_namespaced_pod_log(
                    pod_name,
                    self.settings.namespace,
                    container="pytorch",
                    tail_lines=tail_lines,
                    timestamps=False,
                )
            )
        except ServiceError as exc:
            if exc.status_code in (400, 404):
                return ""
            raise

    def list_events(self, object_names: list[str]) -> list[dict[str, Any]]:
        """Return events for the given involved objects, newest last.

        CoreV1 Event field selectors only support a single exact match per
        field, so this issues one query per object name and de-duplicates by
        event UID afterwards.
        """
        collected: dict[str, dict[str, Any]] = {}
        for name in dict.fromkeys(object_names):
            if not name:
                continue
            response = self._call(
                lambda object_name=name: self._core_api().list_namespaced_event(
                    self.settings.namespace,
                    field_selector=f"involvedObject.name={object_name}",
                )
            )
            for item in response.items:
                event = item.to_dict()
                uid = event.get("metadata", {}).get("uid") or repr(event)
                collected[uid] = event
        return list(collected.values())

    def list_nodes(self) -> list[dict[str, Any]]:
        response = self._call(lambda: self._core_api().list_node())
        return [item.to_dict() for item in response.items]

    def list_training_pods(self) -> list[dict[str, Any]]:
        response = self._call(
            lambda: self._core_api().list_namespaced_pod(self.settings.namespace)
        )
        return [item.to_dict() for item in response.items]

    def get_local_queue(self) -> dict[str, Any] | None:
        try:
            return self._call(
                lambda: self._custom_api().get_namespaced_custom_object(
                    KUEUE_GROUP,
                    KUEUE_VERSION,
                    self.settings.namespace,
                    "localqueues",
                    self.settings.queue,
                )
            )
        except ServiceError as exc:
            if exc.status_code == 404:
                return None
            raise

    def get_cluster_queue(self, name: str) -> dict[str, Any] | None:
        try:
            return self._call(
                lambda: self._custom_api().get_cluster_custom_object(
                    KUEUE_GROUP, KUEUE_VERSION, "clusterqueues", name
                )
            )
        except ServiceError as exc:
            if exc.status_code == 404:
                return None
            raise


def _api_exception_message(exc: ApiException) -> str:
    if exc.reason:
        return f"{exc.status} {exc.reason}"
    return f"Kubernetes request failed with status {exc.status}"
