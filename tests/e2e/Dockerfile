# A disposable Linux remote for gekong's end-to-end tests: sshd + python3 + tmux + rg.
FROM ubuntu:24.04
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      openssh-server python3 tmux ripgrep rsync procps \
 && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /run/sshd \
 && useradd -m -s /bin/bash tester
CMD ["/usr/sbin/sshd", "-D", "-e"]
