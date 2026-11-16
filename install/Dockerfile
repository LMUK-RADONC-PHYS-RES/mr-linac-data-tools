# Build from convert_dump_viewray/:
#   docker build -f install/Dockerfile -t mridian-dump .
# Run (data folder mounted at /data):
#   docker run --rm -v /path/to/data:/data mridian-dump decode_message_dump.py /data/Message.dump
#   docker run --rm -v /path/to/data:/data mridian-dump cine_to_video.py /data/output_conversion/cine --speed 4
# Rootless Docker (as on this server) writes files as your host user, so do not pass -u.
# With rootful Docker, add: -u $(id -u):$(id -g) --group-add $(getent group mridian | cut -d: -f3)
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 MPLCONFIGDIR=/tmp/matplotlib

WORKDIR /app
COPY install/requirements.txt .
RUN pip install --no-cache-dir --root-user-action=ignore -r requirements.txt
COPY --chmod=644 decode_message_dump.py cine_to_video.py ./

ENTRYPOINT ["python", "-I"]
CMD ["decode_message_dump.py", "--help"]
