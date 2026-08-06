# Test fixture: a machine that has both sshd and Hermes.
#
# The end-to-end migration smoke test needs a target it can actually restore
# into, which neither existing image provides: the sshd fixture
# (linuxserver/openssh-server) has no python3 or hermes, and the Hermes images
# have no sshd. This is that image, and it is *only* a test fixture — nothing
# ships it.
#
# Build:
#   docker build -f docker/migration-target.Dockerfile -t localhost/hermes-migration-target .
#
# Run (public key is the one the test authenticates with):
#   docker run -d --name hermes-migrate-target -p 2222:22 \
#     -e AUTHORIZED_KEY="$(cat id_ed25519.pub)" localhost/hermes-migration-target
FROM localhost/hermes-agent:latest

USER root

RUN apt-get -o Acquire::Retries=3 update && \
    apt-get -o Acquire::Retries=3 install -y --no-install-recommends \
        openssh-server && \
    rm -rf /var/lib/apt/lists/*

# `command -v hermes` decides whether the migration installs Hermes, and it runs
# in a non-interactive ssh shell that never sources a profile. The symlink is
# what makes the install stage correctly skip.
RUN ln -sf /opt/hermes/bin/hermes /usr/local/bin/hermes

# The base image already provides the hermes user (home: /opt/data).

COPY docker/migration-target-entrypoint.sh /usr/local/bin/migration-target-entrypoint.sh
RUN chmod +x /usr/local/bin/migration-target-entrypoint.sh

EXPOSE 22
ENTRYPOINT ["/usr/local/bin/migration-target-entrypoint.sh"]
