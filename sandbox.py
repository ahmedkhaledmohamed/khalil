"""Container sandbox for running untrusted generated code in Docker."""

import hashlib
import logging
from pathlib import Path

import docker

from config import PHAROCLAW_DIR, SANDBOX_IMAGE, SANDBOX_MEM_LIMIT, SANDBOX_TIMEOUT

log = logging.getLogger("pharoclaw.sandbox")


def is_docker_available() -> bool:
    """Check if Docker daemon is running. Returns False gracefully."""
    try:
        client = docker.from_env()
        client.ping()
        return True
    except Exception:
        return False


def _requirements_hash() -> str:
    """SHA256 of requirements.txt for image staleness detection."""
    req_file = PHAROCLAW_DIR / "requirements.txt"
    return hashlib.sha256(req_file.read_bytes()).hexdigest()[:12]


def ensure_image() -> bool:
    """Build or verify the pharoclaw-sandbox Docker image. Returns True if ready."""
    if not is_docker_available():
        return False

    client = docker.from_env()
    req_hash = _requirements_hash()

    # Check if image exists with matching hash
    try:
        image = client.images.get(f"{SANDBOX_IMAGE}:latest")
        if image.labels.get("pharoclaw.req_hash") == req_hash:
            return True
        log.info("Sandbox image stale (req_hash mismatch), rebuilding...")
    except docker.errors.ImageNotFound:
        log.info("Sandbox image not found, building...")

    # Build from Dockerfile.sandbox
    dockerfile_path = PHAROCLAW_DIR / "Dockerfile.sandbox"
    if not dockerfile_path.exists():
        log.error("Dockerfile.sandbox not found at %s", dockerfile_path)
        return False

    try:
        client.images.build(
            path=str(PHAROCLAW_DIR),
            dockerfile="Dockerfile.sandbox",
            tag=f"{SANDBOX_IMAGE}:latest",
            buildargs={},
            labels={"pharoclaw.req_hash": req_hash},
            rm=True,
        )
        log.info("Sandbox image built successfully (hash=%s)", req_hash)
        return True
    except Exception as e:
        log.error("Failed to build sandbox image: %s", e)
        return False


def run_in_sandbox(
    script: str,
    timeout: int = SANDBOX_TIMEOUT,
    network: bool = False,
    mount_pharoclaw: bool = True,
) -> tuple[int, str, str]:
    """Run a Python script in an ephemeral Docker container.

    Args:
        script: Python code (passed via python -c)
        timeout: Container timeout in seconds
        network: Allow network access (default False)
        mount_pharoclaw: Mount pharoclaw repo read-only at /pharoclaw

    Returns:
        (exit_code, stdout, stderr)

    Raises:
        RuntimeError: If Docker not available or image not built
    """
    if not ensure_image():
        raise RuntimeError("Sandbox image not available")

    client = docker.from_env()

    volumes = {}
    if mount_pharoclaw:
        volumes[str(PHAROCLAW_DIR)] = {"bind": "/pharoclaw", "mode": "ro"}

    container = None
    try:
        container = client.containers.run(
            f"{SANDBOX_IMAGE}:latest",
            command=["python", "-c", script],
            volumes=volumes,
            mem_limit=SANDBOX_MEM_LIMIT,
            nano_cpus=1_000_000_000,  # 1 CPU
            network_disabled=not network,
            read_only=True,
            tmpfs={"/tmp": "size=10m"},
            detach=True,
            stdout=True,
            stderr=True,
        )

        result = container.wait(timeout=timeout)
        exit_code = result.get("StatusCode", -1)
        stdout = container.logs(stdout=True, stderr=False).decode("utf-8", errors="replace")
        stderr = container.logs(stdout=False, stderr=True).decode("utf-8", errors="replace")

        return exit_code, stdout.strip(), stderr.strip()

    except Exception as e:
        log.error("Sandbox execution failed: %s", e)
        return -1, "", str(e)

    finally:
        if container:
            try:
                container.remove(force=True)
            except Exception:
                pass
