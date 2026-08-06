#!/bin/sh
# Entry point for the migration test target: authorize one key, run sshd.
#
# Test fixture only. It authorizes whatever key is handed to it and runs sshd in
# the foreground, which is fine for a throwaway container and nothing else.
set -eu

if [ -z "${AUTHORIZED_KEY:-}" ]; then
    echo "AUTHORIZED_KEY is required" >&2
    exit 2
fi

# The base image's hermes user does not live in /home/hermes — ask, do not
# assume, or sshd closes the connection with nothing but "Connection closed".
HERMES_HOMEDIR="$(getent passwd hermes | cut -d: -f6)"
install -d -m 700 -o hermes -g hermes "$HERMES_HOMEDIR/.ssh"
printf '%s\n' "$AUTHORIZED_KEY" > "$HERMES_HOMEDIR/.ssh/authorized_keys"
chmod 600 "$HERMES_HOMEDIR/.ssh/authorized_keys"
chown hermes:hermes "$HERMES_HOMEDIR/.ssh/authorized_keys"

# Host keys are generated per container, so every run is a first contact — which
# is exactly what the pinning path should see.
ssh-keygen -A >/dev/null
install -d -m 755 /run/sshd

exec /usr/sbin/sshd -D -e
