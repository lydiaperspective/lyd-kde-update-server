FROM docker.io/library/archlinux:base-devel
COPY bootstrap.sh /tmp/bootstrap.sh
RUN chmod +x /tmp/bootstrap.sh && /tmp/bootstrap.sh && rm /tmp/bootstrap.sh
RUN groupadd -g 1000 users || true && useradd -m -u 1000 -g users -s /bin/bash lydia && echo 'lydia ALL=(ALL) NOPASSWD: ALL' >> /etc/sudoers
WORKDIR /workspace
COPY builder/worker.py /usr/local/bin/kde-update-worker.py
COPY builder/build-iso-cancel.patch /usr/local/share/kde-update-manager/build-iso-cancel.patch
RUN chmod 755 /usr/local/bin/kde-update-worker.py
ENTRYPOINT ["python", "/usr/local/bin/kde-update-worker.py"]
