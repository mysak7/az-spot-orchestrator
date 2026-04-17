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
        write_files:
          - path: /usr/local/bin/eviction-monitor.sh
            permissions: '0755'
            content: |
              #!/bin/bash
              # Poll Azure IMDS scheduledevents every 15 s.
              # When a Preempt event is detected, notify the control plane so it
              # can provision a replacement VM before this one disappears.
              IMDS_URL="http://169.254.169.254/metadata/scheduledevents?api-version=2020-07-01"
              CONTROL_PLANE="{control_plane_url}"
              VM_NAME="{vm_name}"
              NOTIFIED=0

              while true; do
                EVENTS=$(curl -sf -H "Metadata: true" "$IMDS_URL" 2>/dev/null)
                if [ $? -eq 0 ] && [ $NOTIFIED -eq 0 ]; then
                  EVENT_TYPE=$(echo "$EVENTS" | python3 -c "
              import sys, json
              data = json.load(sys.stdin)
              for e in data.get('Events', []):
                  if e.get('EventType') == 'Preempt':
                      print('Preempt')
                      break
              " 2>/dev/null)
                  if [ "$EVENT_TYPE" = "Preempt" ]; then
                    curl -sf -X POST "$CONTROL_PLANE/api/vms/$VM_NAME/evicted" \
                      -H "Content-Type: application/json" \
                      -d '{{"reason":"spot_preempt"}}' || true
                    NOTIFIED=1
                  fi
                fi
                sleep 15
              done

          - path: /etc/systemd/system/eviction-monitor.service
            content: |
              [Unit]
              Description=Azure Spot eviction monitor
              After=network-online.target
              Wants=network-online.target

              [Service]
              Type=simple
              ExecStart=/usr/local/bin/eviction-monitor.sh
              Restart=always
              RestartSec=5

              [Install]
              WantedBy=multi-user.target

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

          # Start eviction monitor before model pull so evictions during download
          # are also caught and reported to the control plane.
          - systemctl enable eviction-monitor
          - systemctl start eviction-monitor

          # Wait for Ollama, restore model from blob cache or pull from Ollama, then notify ready.
          - |
            # --- Wait for Ollama to accept connections (up to 5 min) ---
            for i in $(seq 1 30); do
              curl -sf http://localhost:11434/api/tags > /dev/null && break
              sleep 10
            done

            # --- Detect this VM's region from IMDS ---
            VM_REGION=$(curl -sf -H "Metadata: true" \
              "http://169.254.169.254/metadata/instance/compute/location?api-version=2021-02-01&format=text" \
              2>/dev/null || echo "")

            # --- Ask the control plane for the model source ---
            SOURCE_RESP=$(curl -sf \
              "{control_plane_url}/api/storage/cache/source?model_identifier={model_identifier}&region=$VM_REGION" \
              2>/dev/null || echo "{{}}")
            SOURCE=$(echo "$SOURCE_RESP" | python3 -c \
              "import sys,json; print(json.load(sys.stdin).get('source','ollama'))" 2>/dev/null || echo "ollama")

            # --- Blob cache path ---
            if [ "$SOURCE" = "blob" ]; then
              echo "=== Restoring model from blob cache ==="
              DOWNLOAD_URL=$(echo "$SOURCE_RESP" | python3 -c \
                "import sys,json; print(json.load(sys.stdin).get('download_url',''))" 2>/dev/null || echo "")

              if [ -n "$DOWNLOAD_URL" ]; then
                if curl -sf -o /tmp/model.tar.lz4 "$DOWNLOAD_URL"; then
                  echo "Extracting model..."
                  lz4cat /tmp/model.tar.lz4 | tar -x -C /mnt/resource/ || true
                  rm -f /tmp/model.tar.lz4
                  echo "Model extracted. Restarting Ollama..."
                  systemctl restart ollama
                  sleep 10
                  SOURCE="done"
                else
                  echo "WARNING: Blob download failed — falling back to Ollama pull"
                  SOURCE="ollama"
                fi
              else
                echo "WARNING: No download URL in cache response — falling back to Ollama pull"
                SOURCE="ollama"
              fi
            fi

            # --- Ollama fallback: pull model ---
            if [ "$SOURCE" = "ollama" ]; then
              echo "=== Pulling model from Ollama ==="
              if ! HOME=/root OLLAMA_HOST=http://localhost:11434 ollama pull {model_identifier}; then
                echo "ERROR: Model pull failed"
                exit 1
              fi
            fi

            # --- Notify control plane that this VM is ready ---
            curl -sf -X POST {control_plane_url}/api/vms/{vm_name}/ready \
              -H 'Content-Type: application/json' \
              -d '{{"status":"running"}}' || true

        # Install curl and lz4 early in the boot sequence
        packages:
          - curl
          - lz4

        package_update: true
    """)
    return base64.b64encode(yaml.encode()).decode()


def generate_cloud_init_with_files_mount(
    model_identifier: str,
    storage_account: str,
    share_name: str,
    account_key: str,
    control_plane_url: str,
    vm_name: str,
) -> str:
    """Return base64-encoded cloud-config for a VM that mounts model weights from Azure Files SMB.

    The SMB share already has the model files; cloud-init just mounts it and starts Ollama.
    Boot-to-ready time: ~2 min instead of 15-45 min (no download).

    Args:
        model_identifier: Ollama model tag — used only for the ready callback.
        storage_account:  Azure FileStorage account name (e.g. "azspotfileswesteurope").
        share_name:       SMB share name (always "models").
        account_key:      Storage account key for CIFS mount authentication.
        control_plane_url: Base URL for the ready callback.
        vm_name:          Unique VM name for the ready callback and eviction monitor.
    """
    smb_server = f"//{storage_account}.file.core.windows.net/{share_name}"

    yaml = textwrap.dedent(f"""\
        #cloud-config

        write_files:
          - path: /etc/smbcredentials/{storage_account}.cred
            permissions: '0600'
            content: |
              username={storage_account}
              password={account_key}

          - path: /usr/local/bin/eviction-monitor.sh
            permissions: '0755'
            content: |
              #!/bin/bash
              IMDS_URL="http://169.254.169.254/metadata/scheduledevents?api-version=2020-07-01"
              CONTROL_PLANE="{control_plane_url}"
              VM_NAME="{vm_name}"
              NOTIFIED=0

              while true; do
                EVENTS=$(curl -sf -H "Metadata: true" "$IMDS_URL" 2>/dev/null)
                if [ $? -eq 0 ] && [ $NOTIFIED -eq 0 ]; then
                  EVENT_TYPE=$(echo "$EVENTS" | python3 -c "
              import sys, json
              data = json.load(sys.stdin)
              for e in data.get('Events', []):
                  if e.get('EventType') == 'Preempt':
                      print('Preempt')
                      break
              " 2>/dev/null)
                  if [ "$EVENT_TYPE" = "Preempt" ]; then
                    curl -sf -X POST "$CONTROL_PLANE/api/vms/$VM_NAME/evicted" \\
                      -H "Content-Type: application/json" \\
                      -d '{{"reason":"spot_preempt"}}' || true
                    NOTIFIED=1
                  fi
                fi
                sleep 15
              done

          - path: /etc/systemd/system/eviction-monitor.service
            content: |
              [Unit]
              Description=Azure Spot eviction monitor
              After=network-online.target
              Wants=network-online.target

              [Service]
              Type=simple
              ExecStart=/usr/local/bin/eviction-monitor.sh
              Restart=always
              RestartSec=5

              [Install]
              WantedBy=multi-user.target

        packages:
          - curl
          - cifs-utils

        package_update: true

        runcmd:
          # Mount Azure Files SMB share — model files already present
          - mkdir -p /etc/smbcredentials
          - mkdir -p /mnt/ollama-files
          - |
            mount -t cifs {smb_server} /mnt/ollama-files \
              -o credentials=/etc/smbcredentials/{storage_account}.cred,dir_mode=0777,file_mode=0777,serverino,nofail,vers=3.0

          # Install Ollama
          - curl -fsSL https://ollama.ai/install.sh | sh

          # Override Ollama service: use SMB mount for models, listen on all interfaces
          - mkdir -p /etc/systemd/system/ollama.service.d
          - |
            printf '[Service]\\nEnvironment="OLLAMA_MODELS=/mnt/ollama-files"\\nEnvironment="OLLAMA_HOST=0.0.0.0:11434"\\n' \
              > /etc/systemd/system/ollama.service.d/override.conf
          - systemctl daemon-reload
          - systemctl enable ollama
          - systemctl start ollama

          - systemctl enable eviction-monitor
          - systemctl start eviction-monitor

          # Wait for Ollama to start, then notify control plane — no download needed
          - |
            for i in $(seq 1 30); do
              curl -sf http://localhost:11434/api/tags > /dev/null && break
              sleep 10
            done
            curl -sf -X POST {control_plane_url}/api/vms/{vm_name}/ready \
              -H 'Content-Type: application/json' \
              -d '{{"status":"running"}}' || true
    """)
    return base64.b64encode(yaml.encode()).decode()


def generate_bare_cloud_init(vm_name: str, control_plane_url: str) -> str:
    """Return base64-encoded cloud-config YAML for a bare SSH-only Spot VM.

    No Ollama. Installs basic utilities and the eviction monitor, then
    notifies the control plane that the VM is ready.
    """
    yaml = textwrap.dedent(f"""\
        #cloud-config

        write_files:
          - path: /usr/local/bin/eviction-monitor.sh
            permissions: '0755'
            content: |
              #!/bin/bash
              IMDS_URL="http://169.254.169.254/metadata/scheduledevents?api-version=2020-07-01"
              CONTROL_PLANE="{control_plane_url}"
              VM_NAME="{vm_name}"
              NOTIFIED=0
              while true; do
                EVENTS=$(curl -sf -H "Metadata: true" "$IMDS_URL" 2>/dev/null)
                if [ $? -eq 0 ] && [ $NOTIFIED -eq 0 ]; then
                  EVENT_TYPE=$(echo "$EVENTS" | python3 -c "
              import sys, json
              data = json.load(sys.stdin)
              for e in data.get('Events', []):
                  if e.get('EventType') == 'Preempt':
                      print('Preempt')
                      break
              " 2>/dev/null)
                  if [ "$EVENT_TYPE" = "Preempt" ]; then
                    curl -sf -X POST "$CONTROL_PLANE/api/vms/$VM_NAME/evicted" \\
                      -H "Content-Type: application/json" \\
                      -d '{{"reason":"spot_preempt"}}' || true
                    NOTIFIED=1
                  fi
                fi
                sleep 15
              done

          - path: /etc/systemd/system/eviction-monitor.service
            content: |
              [Unit]
              Description=Azure Spot eviction monitor
              After=network-online.target
              Wants=network-online.target

              [Service]
              Type=simple
              ExecStart=/usr/local/bin/eviction-monitor.sh
              Restart=always
              RestartSec=5

              [Install]
              WantedBy=multi-user.target

        packages:
          - curl
          - htop
          - tmux
          - jq
          - nvtop

        package_update: true

        runcmd:
          - systemctl enable eviction-monitor
          - systemctl start eviction-monitor
          - curl -sf -X POST {control_plane_url}/api/vms/{vm_name}/ready
              -H 'Content-Type: application/json'
              -d '{{"status":"running"}}' || true
    """)
    return base64.b64encode(yaml.encode()).decode()
