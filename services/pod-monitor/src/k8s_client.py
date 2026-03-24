"""Kubernetes API client wrapper for pod monitoring."""

import asyncio
import logging
from typing import Any, Dict, List, Optional
from datetime import datetime, timezone

from kubernetes import client, config
from kubernetes.client.rest import ApiException

logger = logging.getLogger(__name__)


class K8sClient:
    """
    Wrapper around the Kubernetes Python client.

    All sync k8s API calls are wrapped in asyncio.to_thread()
    to avoid blocking the aiohttp event loop.
    """

    def __init__(self):
        try:
            config.load_incluster_config()
            logger.info("Loaded in-cluster Kubernetes config")
        except config.ConfigException:
            try:
                config.load_kube_config()
                logger.info("Loaded local kubeconfig")
            except config.ConfigException:
                logger.error("Could not load Kubernetes config")
                raise

        self._v1 = client.CoreV1Api()
        self._custom = client.CustomObjectsApi()

    async def list_pods(self, namespace: str = "default") -> List[Dict[str, Any]]:
        """List all pods in a namespace with status info."""
        pods = await asyncio.to_thread(
            self._v1.list_namespaced_pod, namespace
        )

        results = []
        for pod in pods.items:
            results.append(self._format_pod_summary(pod))

        return sorted(results, key=lambda p: p["name"])

    async def get_pod_details(self, pod_name: str, namespace: str = "default") -> Optional[Dict[str, Any]]:
        """Get detailed info for a specific pod (supports fuzzy matching)."""
        # Try exact match first
        try:
            pod = await asyncio.to_thread(
                self._v1.read_namespaced_pod, pod_name, namespace
            )
            details = self._format_pod_details(pod)
            # Fetch events for this pod
            details["events"] = await self._get_pod_events(pod.metadata.name, namespace)
            return details
        except ApiException as e:
            if e.status != 404:
                raise

        # Fuzzy match: find pods whose name contains the search term
        pods = await asyncio.to_thread(
            self._v1.list_namespaced_pod, namespace
        )
        matches = [
            p for p in pods.items
            if pod_name.lower() in p.metadata.name.lower()
        ]

        if not matches:
            return None
        if len(matches) == 1:
            details = self._format_pod_details(matches[0])
            details["events"] = await self._get_pod_events(matches[0].metadata.name, namespace)
            return details

        # Multiple matches - return the first one but note alternatives
        details = self._format_pod_details(matches[0])
        details["events"] = await self._get_pod_events(matches[0].metadata.name, namespace)
        details["other_matches"] = [m.metadata.name for m in matches[1:5]]
        return details

    async def get_pod_logs(
        self,
        pod_name: str,
        namespace: str = "default",
        lines: int = 100,
        container: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Get recent logs from a pod (supports fuzzy matching)."""
        # Resolve pod name (fuzzy match)
        resolved_name = await self._resolve_pod_name(pod_name, namespace)
        if not resolved_name:
            return {"error": f"No pod found matching '{pod_name}'", "logs": ""}

        try:
            kwargs = {
                "name": resolved_name,
                "namespace": namespace,
                "tail_lines": lines,
            }
            if container:
                kwargs["container"] = container

            logs = await asyncio.to_thread(
                self._v1.read_namespaced_pod_log, **kwargs
            )
            return {
                "pod": resolved_name,
                "logs": logs or "(no logs)",
                "lines": lines,
                "container": container,
            }
        except ApiException as e:
            return {
                "pod": resolved_name,
                "error": f"Failed to get logs: {e.reason}",
                "logs": "",
            }

    async def list_unhealthy_pods(self, namespace: str = "default") -> List[Dict[str, Any]]:
        """List pods that are not healthy."""
        pods = await asyncio.to_thread(
            self._v1.list_namespaced_pod, namespace
        )

        unhealthy = []
        for pod in pods.items:
            issues = self._check_pod_health(pod)
            if issues:
                summary = self._format_pod_summary(pod)
                summary["issues"] = issues
                unhealthy.append(summary)

        return unhealthy

    async def get_namespace_summary(self, namespace: str = "default") -> Dict[str, Any]:
        """Get an overview of a namespace: pod counts, health, resources."""
        pods = await asyncio.to_thread(
            self._v1.list_namespaced_pod, namespace
        )

        total = len(pods.items)
        running = 0
        pending = 0
        failed = 0
        succeeded = 0
        unhealthy = 0
        total_restarts = 0
        total_containers = 0

        status_breakdown = {}

        for pod in pods.items:
            phase = pod.status.phase or "Unknown"
            status_breakdown[phase] = status_breakdown.get(phase, 0) + 1

            if phase == "Running":
                running += 1
            elif phase == "Pending":
                pending += 1
            elif phase == "Failed":
                failed += 1
            elif phase == "Succeeded":
                succeeded += 1

            if pod.status.container_statuses:
                for cs in pod.status.container_statuses:
                    total_containers += 1
                    total_restarts += cs.restart_count or 0

            issues = self._check_pod_health(pod)
            if issues:
                unhealthy += 1

        # Try to get resource metrics
        metrics = await self._get_namespace_metrics(namespace)

        return {
            "namespace": namespace,
            "total_pods": total,
            "running": running,
            "pending": pending,
            "failed": failed,
            "succeeded": succeeded,
            "unhealthy": unhealthy,
            "total_containers": total_containers,
            "total_restarts": total_restarts,
            "status_breakdown": status_breakdown,
            "metrics": metrics,
        }

    async def _resolve_pod_name(self, pod_name: str, namespace: str) -> Optional[str]:
        """Resolve a potentially partial pod name to a full pod name."""
        # Try exact match
        try:
            await asyncio.to_thread(
                self._v1.read_namespaced_pod, pod_name, namespace
            )
            return pod_name
        except ApiException:
            pass

        # Fuzzy match
        pods = await asyncio.to_thread(
            self._v1.list_namespaced_pod, namespace
        )
        matches = [
            p.metadata.name for p in pods.items
            if pod_name.lower() in p.metadata.name.lower()
        ]

        if matches:
            return matches[0]
        return None

    async def _get_pod_events(self, pod_name: str, namespace: str) -> List[Dict[str, str]]:
        """Get recent events for a pod."""
        try:
            field_selector = f"involvedObject.name={pod_name}"
            events = await asyncio.to_thread(
                self._v1.list_namespaced_event,
                namespace,
                field_selector=field_selector,
            )
            result = []
            for event in events.items:
                result.append({
                    "type": event.type or "Normal",
                    "reason": event.reason or "",
                    "message": event.message or "",
                    "count": str(event.count or 1),
                    "last_seen": self._format_age(event.last_timestamp),
                })
            # Sort by last seen, most recent first
            return result[-10:]  # Last 10 events
        except Exception as e:
            logger.warning(f"Failed to get events for {pod_name}: {e}")
            return []

    async def _get_namespace_metrics(self, namespace: str) -> Optional[Dict[str, Any]]:
        """Get resource metrics for a namespace (requires metrics-server)."""
        try:
            metrics = await asyncio.to_thread(
                self._custom.list_namespaced_custom_object,
                group="metrics.k8s.io",
                version="v1beta1",
                namespace=namespace,
                plural="pods",
            )

            total_cpu_millicores = 0
            total_memory_bytes = 0

            for pod_metric in metrics.get("items", []):
                for container in pod_metric.get("containers", []):
                    usage = container.get("usage", {})
                    cpu = usage.get("cpu", "0")
                    mem = usage.get("memory", "0")

                    total_cpu_millicores += self._parse_cpu(cpu)
                    total_memory_bytes += self._parse_memory(mem)

            return {
                "total_cpu": f"{total_cpu_millicores}m",
                "total_memory": self._format_bytes(total_memory_bytes),
            }
        except Exception:
            # Metrics server not available
            return None

    def _format_pod_summary(self, pod) -> Dict[str, Any]:
        """Format a pod object into a summary dict."""
        restarts = 0
        ready_count = 0
        total_count = 0
        container_statuses = []

        if pod.status.container_statuses:
            for cs in pod.status.container_statuses:
                total_count += 1
                restarts += cs.restart_count or 0
                if cs.ready:
                    ready_count += 1

                state = "unknown"
                state_detail = ""
                if cs.state:
                    if cs.state.running:
                        state = "running"
                    elif cs.state.waiting:
                        state = "waiting"
                        state_detail = cs.state.waiting.reason or ""
                    elif cs.state.terminated:
                        state = "terminated"
                        state_detail = cs.state.terminated.reason or ""

                container_statuses.append({
                    "name": cs.name,
                    "state": state,
                    "state_detail": state_detail,
                    "ready": cs.ready,
                    "restart_count": cs.restart_count or 0,
                })

        return {
            "name": pod.metadata.name,
            "namespace": pod.metadata.namespace,
            "phase": pod.status.phase or "Unknown",
            "ready": f"{ready_count}/{total_count}",
            "restarts": restarts,
            "age": self._format_age(pod.metadata.creation_timestamp),
            "node": pod.spec.node_name or "unscheduled",
            "containers": container_statuses,
        }

    def _format_pod_details(self, pod) -> Dict[str, Any]:
        """Format a pod object into a detailed dict."""
        summary = self._format_pod_summary(pod)

        # Add image info
        images = []
        if pod.spec.containers:
            for c in pod.spec.containers:
                images.append({
                    "container": c.name,
                    "image": c.image,
                })

        # Add resource requests/limits
        resources = []
        if pod.spec.containers:
            for c in pod.spec.containers:
                res = {"container": c.name}
                if c.resources:
                    if c.resources.requests:
                        res["requests"] = {
                            "cpu": c.resources.requests.get("cpu", "-"),
                            "memory": c.resources.requests.get("memory", "-"),
                        }
                    if c.resources.limits:
                        res["limits"] = {
                            "cpu": c.resources.limits.get("cpu", "-"),
                            "memory": c.resources.limits.get("memory", "-"),
                        }
                resources.append(res)

        # Add conditions
        conditions = []
        if pod.status.conditions:
            for cond in pod.status.conditions:
                conditions.append({
                    "type": cond.type,
                    "status": cond.status,
                    "reason": cond.reason or "",
                })

        summary.update({
            "images": images,
            "resources": resources,
            "conditions": conditions,
            "ip": pod.status.pod_ip or "none",
            "service_account": pod.spec.service_account_name or "default",
        })

        return summary

    def _check_pod_health(self, pod) -> List[str]:
        """Check a pod for health issues. Returns list of issue descriptions."""
        issues = []
        now = datetime.now(timezone.utc)

        phase = pod.status.phase or "Unknown"

        # Check for stuck Pending
        if phase == "Pending":
            created = pod.metadata.creation_timestamp
            if created and (now - created).total_seconds() > 300:
                issues.append("Stuck in Pending for >5 min")

        # Check for Failed phase
        if phase == "Failed":
            issues.append(f"Pod failed: {pod.status.reason or 'unknown reason'}")

        # Check container statuses
        if pod.status.container_statuses:
            for cs in pod.status.container_statuses:
                # High restarts — only flag if the last restart was recent (within 30 min)
                # K8s restart counts never reset, so old accumulated restarts are not actionable
                if (cs.restart_count or 0) > 5:
                    last_terminated = cs.last_state and cs.last_state.terminated
                    if last_terminated and last_terminated.finished_at:
                        finished = last_terminated.finished_at
                        if finished.tzinfo is None:
                            finished = finished.replace(tzinfo=timezone.utc)
                        minutes_since = (now - finished).total_seconds() / 60
                        if minutes_since < 30:
                            issues.append(f"{cs.name}: {cs.restart_count} restarts (last {int(minutes_since)}m ago)")
                    elif not cs.ready:
                        # No last_state info but container isn't ready — flag it
                        issues.append(f"{cs.name}: {cs.restart_count} restarts")

                if cs.state:
                    # CrashLoopBackOff
                    if cs.state.waiting and cs.state.waiting.reason == "CrashLoopBackOff":
                        issues.append(f"{cs.name}: CrashLoopBackOff")

                    # OOMKilled
                    if cs.state.terminated and cs.state.terminated.reason == "OOMKilled":
                        issues.append(f"{cs.name}: OOMKilled")

                    # ImagePullBackOff
                    if cs.state.waiting and cs.state.waiting.reason in ("ImagePullBackOff", "ErrImagePull"):
                        issues.append(f"{cs.name}: {cs.state.waiting.reason}")

                # Not ready for >5 min (only for Running pods)
                # Use the Ready condition's lastTransitionTime to check how long
                # it's actually been not-ready, not just pod age
                if phase == "Running" and not cs.ready:
                    not_ready_since = None
                    if pod.status.conditions:
                        for cond in pod.status.conditions:
                            if cond.type == "Ready" and cond.status == "False":
                                not_ready_since = cond.last_transition_time
                                break
                    if not_ready_since:
                        if not_ready_since.tzinfo is None:
                            not_ready_since = not_ready_since.replace(tzinfo=timezone.utc)
                        if (now - not_ready_since).total_seconds() > 300:
                            issues.append(f"{cs.name}: not ready for >5 min")

        return issues

    def _format_age(self, timestamp) -> str:
        """Format a timestamp into a human-readable age string."""
        if not timestamp:
            return "unknown"

        try:
            now = datetime.now(timezone.utc)
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)

            delta = now - timestamp
            seconds = int(delta.total_seconds())

            if seconds < 60:
                return f"{seconds}s"
            elif seconds < 3600:
                return f"{seconds // 60}m"
            elif seconds < 86400:
                hours = seconds // 3600
                minutes = (seconds % 3600) // 60
                return f"{hours}h{minutes}m" if minutes else f"{hours}h"
            else:
                days = seconds // 86400
                hours = (seconds % 86400) // 3600
                return f"{days}d{hours}h" if hours else f"{days}d"
        except Exception:
            return "unknown"

    @staticmethod
    def _parse_cpu(cpu_str: str) -> int:
        """Parse CPU string to millicores."""
        if cpu_str.endswith("n"):
            return int(cpu_str[:-1]) // 1_000_000
        elif cpu_str.endswith("u"):
            return int(cpu_str[:-1]) // 1_000
        elif cpu_str.endswith("m"):
            return int(cpu_str[:-1])
        else:
            try:
                return int(float(cpu_str) * 1000)
            except ValueError:
                return 0

    @staticmethod
    def _parse_memory(mem_str: str) -> int:
        """Parse memory string to bytes."""
        suffixes = {
            "Ki": 1024,
            "Mi": 1024 ** 2,
            "Gi": 1024 ** 3,
            "Ti": 1024 ** 4,
            "K": 1000,
            "M": 1000 ** 2,
            "G": 1000 ** 3,
        }
        for suffix, multiplier in suffixes.items():
            if mem_str.endswith(suffix):
                try:
                    return int(float(mem_str[:-len(suffix)]) * multiplier)
                except ValueError:
                    return 0
        try:
            return int(mem_str)
        except ValueError:
            return 0

    @staticmethod
    def _format_bytes(num_bytes: int) -> str:
        """Format bytes into human-readable string."""
        if num_bytes < 1024:
            return f"{num_bytes}B"
        elif num_bytes < 1024 ** 2:
            return f"{num_bytes / 1024:.1f}Ki"
        elif num_bytes < 1024 ** 3:
            return f"{num_bytes / (1024**2):.1f}Mi"
        else:
            return f"{num_bytes / (1024**3):.1f}Gi"
