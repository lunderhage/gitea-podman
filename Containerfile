FROM docker.io/library/alpine:3.22
RUN apk add --no-cache python3 podman podman-compose rclone tar
COPY scripts/manager.py /opt/gitea/manager.py
COPY scripts/backup_service.py scripts/dump_restore.py /opt/gitea/
ENTRYPOINT ["python3", "/opt/gitea/manager.py"]
