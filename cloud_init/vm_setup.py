"""Generates the base64-encoded cloud-init YAML injected into Azure Spot VMs.

The cloud-init script:
  1. Uses the Azure temp disk (/mnt/resource) for model storage — it's free,
     ephemeral NVMe/SSD storage that disappears on deallocation, which is fine
     because Spot VMs are reprovisioned from scratch after eviction anyway.
  2. Installs Ollama and points OLLAMA_MODELS at the temp disk path.
  3. Pulls the requested model via a one-shot systemd service.
  4. Notifies the control plane when the model is ready.
"""
from __future__ import annotations

import base64
import textwrap


def generate_cloud_init(
    model_identifier: str,
    control_plane_url: str,
    vm_name: str,
) -> str:
    """Return base64-encoded cloud-config YAML for an Ollama inference VM.

    Args:
        model_identifier: Ollama model tag (e.g. "llama3:8b").
        control_plane_url: Base URL of the control-plane API for the ready callback.
        vm_name: Unique VM name; sent in the ready callback.

    Returns:
        Base64 string suitable for Azure's ``custom_data`` field.
    """
    yaml = textwrap.dedent(f"""\
        #cloud-config

        # Azure Linux VMs mount the temp disk at /mnt/resource by default.
        # We create a sub-directory there for Ollama model weights.
        runcmd:
          - mkdir -p /mnt/resource/models

          # Install Ollama (creates the 'ollama' system user)
          - curl -fsSL https://ollama.ai/install.sh | sh

          # Grant the ollama user ownership of the model directory
          - chown -R ollama:ollama /mnt/resource/models

          # Override Ollama service to use temp disk and listen on all interfaces
          - mkdir -p /etc/systemd/system/ollama.service.d
          - |
            printf '[Service]\\nEnvironment="OLLAMA_MODELS=/mnt/resource/models"\\nEnvironment="OLLAMA_HOST=0.0.0.0:11434"\\n' \
              > /etc/systemd/system/ollama.service.d/override.conf
          - systemctl daemon-reload
          - systemctl enable ollama
          - systemctl start ollama

          # Wait for Ollama to accept connections (up to 5 min)
          - |
            for i in $(seq 1 30); do
              curl -sf http://localhost:11434/api/tags > /dev/null && break
              sleep 10
            done

          # Pull the model — this blocks until download is complete
          - ollama pull {model_identifier}

          # Notify control plane that this VM is ready
          - |
            curl -sf -X POST {control_plane_url}/api/vms/{vm_name}/ready \
              -H 'Content-Type: application/json' \
              -d '{{"status":"running"}}' || true

        # Install curl early in the boot sequence
        packages:
          - curl

        package_update: true
    """)
    return base64.b64encode(yaml.encode()).decode()
