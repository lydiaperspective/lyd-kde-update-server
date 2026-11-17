FROM docker.io/library/archlinux:base-devel

# Fetch bootstrap.sh directly from your nvidia580xx-legacy branch
COPY bootstrap.sh /tmp/bootstrap.sh

RUN chmod +x /tmp/bootstrap.sh && /tmp/bootstrap.sh && rm /tmp/bootstrap.sh

# Setup build user
RUN groupadd -g 1000 users || true && \
    useradd -m -u 1000 -g users -s /bin/bash lydia && \
    echo "lydia ALL=(ALL) NOPASSWD: ALL" >> /etc/sudoers

WORKDIR /workspace

COPY runner.sh /usr/local/bin/runner.sh
RUN chmod +x /usr/local/bin/runner.sh

ENTRYPOINT ["/bin/bash", "/usr/local/bin/runner.sh"]
