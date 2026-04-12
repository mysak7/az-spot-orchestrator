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

          # Wait for Ollama, pull or restore model from cache, then notify ready.
          # All in one block so variables (VM_REGION, SOURCE_RESP, UPLOAD_URL) persist.
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

            # --- Ask the control plane for the best model source ---
            SOURCE_RESP=$(curl -sf \
              "{control_plane_url}/api/storage/cache/source?model_identifier={model_identifier}&region=$VM_REGION" \
              2>/dev/null || echo "{{}}")
            SOURCE=$(echo "$SOURCE_RESP" | python3 -c \
              "import sys,json; print(json.load(sys.stdin).get('source','ollama'))" 2>/dev/null || echo "ollama")
            UPLOAD_URL=$(echo "$SOURCE_RESP" | python3 -c \
              "import sys,json; print(json.load(sys.stdin).get('upload_url',''))" 2>/dev/null || echo "")

            # --- Blob cache path ---
            if [ "$SOURCE" != "ollama" ]; then
              echo "=== Model cache source: $SOURCE ==="
              DOWNLOAD_URL=$(echo "$SOURCE_RESP" | python3 -c \
                "import sys,json; print(json.load(sys.stdin).get('download_url',''))" 2>/dev/null || echo "")
              SOURCE_REGION=$(echo "$SOURCE_RESP" | python3 -c \
                "import sys,json; print(json.load(sys.stdin).get('source_region',''))" 2>/dev/null || echo "")

              if [ -n "$DOWNLOAD_URL" ]; then
                echo "Downloading model from $SOURCE_REGION..."
                START=$(date +%s)
                if curl -sf -o /tmp/model.tar.gz "$DOWNLOAD_URL"; then
                  echo "Extracting model..."
                  tar -xzf /tmp/model.tar.gz -C /mnt/resource/ || true
                  rm -f /tmp/model.tar.gz
                  echo "Model extracted. Restarting Ollama..."
                  systemctl restart ollama
                  sleep 10
                  END=$(date +%s)
                  DURATION=$((END - START))
                  echo "Download and import took $DURATION seconds"
                  curl -sf -X POST "{control_plane_url}/api/storage/cache/download-log" \
                    -H "Content-Type: application/json" \
                    -d "{{\\"model_identifier\\":\\"{model_identifier}\\",\\"region\\":\\"$VM_REGION\\",\\"source_region\\":\\"$SOURCE_REGION\\",\\"duration_seconds\\":$DURATION}}" 2>/dev/null || true
                  SOURCE="blob_done"
                else
                  # Blob download failed (expired SAS, missing blob, network error).
                  # Fall through to Ollama so the VM still becomes usable.
                  echo "WARNING: Blob download failed — falling back to Ollama pull"
                  SOURCE="ollama"
                fi
              else
                echo "WARNING: No download URL in cache response — falling back to Ollama pull"
                SOURCE="ollama"
              fi
            fi

            # --- Ollama fallback: pull then upload to blob ---
            if [ "$SOURCE" = "ollama" ]; then
              echo "=== Pulling model from Ollama (phase: pulling) ==="
              curl -sf -X POST "{control_plane_url}/api/storage/cache/progress" \
                -H "Content-Type: application/json" \
                -d "{{\\"model_identifier\\":\\"{model_identifier}\\",\\"region\\":\\"$VM_REGION\\",\\"phase\\":\\"pulling\\"}}" 2>/dev/null || true

              if ! ollama pull {model_identifier}; then
                echo "ERROR: Model pull failed"
                curl -sf -X POST "{control_plane_url}/api/storage/cache/failed" \
                  -H "Content-Type: application/json" \
                  -d "{{\\"model_identifier\\":\\"{model_identifier}\\",\\"region\\":\\"$VM_REGION\\"}}" 2>/dev/null || true
                exit 1
              fi

              if [ -n "$UPLOAD_URL" ]; then
                echo "=== Archiving model (phase: archiving) ==="
                curl -sf -X POST "{control_plane_url}/api/storage/cache/progress" \
                  -H "Content-Type: application/json" \
                  -d "{{\\"model_identifier\\":\\"{model_identifier}\\",\\"region\\":\\"$VM_REGION\\",\\"phase\\":\\"archiving\\"}}" 2>/dev/null || true

                # Install azcopy — needed for blobs > 250 MB (most real models).
                # Extract the binary by name to avoid --wildcards portability issues.
                mkdir -p /tmp/azcopy_dl
                if curl -sL "https://aka.ms/downloadazcopy-v10-linux" -o /tmp/azcopy_dl/azcopy.tgz 2>/dev/null; then
                  tar -xzf /tmp/azcopy_dl/azcopy.tgz -C /tmp/azcopy_dl/ 2>/dev/null || true
                  AZCOPY_BIN=$(find /tmp/azcopy_dl -name 'azcopy' -type f 2>/dev/null | head -1)
                  if [ -n "$AZCOPY_BIN" ]; then
                    mv "$AZCOPY_BIN" /usr/local/bin/azcopy && chmod +x /usr/local/bin/azcopy
                  fi
                fi
                rm -rf /tmp/azcopy_dl

                START=$(date +%s)
                echo "Creating model archive..."
                tar -czf /tmp/model.tar.gz -C /mnt/resource models/ 2>/dev/null || true

                UPLOAD_SUCCESS=0
                if [ -f /tmp/model.tar.gz ]; then
                  SIZE=$(stat -c%s /tmp/model.tar.gz 2>/dev/null || echo 0)
                  echo "=== Uploading $SIZE bytes to blob (phase: uploading) ==="
                  curl -sf -X POST "{control_plane_url}/api/storage/cache/progress" \
                    -H "Content-Type: application/json" \
                    -d "{{\\"model_identifier\\":\\"{model_identifier}\\",\\"region\\":\\"$VM_REGION\\",\\"phase\\":\\"uploading\\"}}" 2>/dev/null || true

                  if command -v azcopy &> /dev/null; then
                    azcopy copy /tmp/model.tar.gz "$UPLOAD_URL" --overwrite=false 2>/dev/null && UPLOAD_SUCCESS=1 || true
                  elif [ "$SIZE" -lt 262144000 ]; then
                    curl -sf -X PUT -H "x-ms-blob-type: BlockBlob" \
                      --data-binary @/tmp/model.tar.gz "$UPLOAD_URL" 2>/dev/null && UPLOAD_SUCCESS=1 || true
                  else
                    echo "WARNING: azcopy unavailable and model too large for single PUT ($SIZE bytes) — upload skipped"
                  fi
                  rm -f /tmp/model.tar.gz
                fi

                END=$(date +%s)
                DURATION=$((END - START))

                if [ "$UPLOAD_SUCCESS" -eq 1 ]; then
                  curl -sf -X POST "{control_plane_url}/api/storage/cache/complete" \
                    -H "Content-Type: application/json" \
                    -d "{{\\"model_identifier\\":\\"{model_identifier}\\",\\"region\\":\\"$VM_REGION\\",\\"size_bytes\\":${{SIZE:-0}},\\"duration_seconds\\":$DURATION}}" 2>/dev/null || true
                else
                  echo "WARNING: Blob upload failed — marking cache entry as failed"
                  curl -sf -X POST "{control_plane_url}/api/storage/cache/failed" \
                    -H "Content-Type: application/json" \
                    -d "{{\\"model_identifier\\":\\"{model_identifier}\\",\\"region\\":\\"$VM_REGION\\"}}" 2>/dev/null || true
                fi
              fi
            fi

            # --- Notify control plane that this VM is ready ---
            curl -sf -X POST {control_plane_url}/api/vms/{vm_name}/ready \
              -H 'Content-Type: application/json' \
              -d '{{"status":"running"}}' || true

        # Install curl early in the boot sequence
        packages:
          - curl

        package_update: true
    """)
    return base64.b64encode(yaml.encode()).decode()
