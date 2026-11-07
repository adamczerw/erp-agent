# Copyright 2025 Alibaba Group Holding Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

FROM ubuntu:22.04

# Default to English; install locale support first, then other deps
ENV DEBIAN_FRONTEND=noninteractive \
    LANG=en_US.UTF-8 \
    LC_ALL=en_US.UTF-8 \
    LANGUAGE=en_US:en

#----------------------
# Install locales first, then remaining dependencies
RUN apt-get update \
    && apt-get install -y locales \
    && locale-gen en_US.UTF-8 \
    && update-locale LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 LANGUAGE=en_US:en \
    && apt-get install -y \
        python3 \
        python3-pip \
        python3-venv \
        python3-dev \
        python3-websockify \
        xvfb \
        x11vnc \
        x11-utils \
        scrot \
        xfce4 \
        xfce4-terminal \
        dbus-x11 \
        xterm \
        novnc \
        fonts-dejavu-core \
        net-tools \
        ca-certificates \
        --no-install-recommends \
    && sed -i 's/DEFAULT_LOCALE = null;/DEFAULT_LOCALE = "en";/' /usr/share/novnc/app/localization.js \
    && rm -rf /var/lib/apt/lists/*

# Install Google Chrome browser (amd64)
RUN apt-get update \
    && apt-get install -y wget gnupg ca-certificates --no-install-recommends \
    && wget -qO- https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /usr/share/keyrings/google-linux.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-linux.gpg] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update \
    && apt-get install -y google-chrome-stable --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

# Precreate X11 stuff
RUN mkdir -p /tmp/.X11-unix
RUN chmod 1777 /tmp/.X11-unix

#----------------------
# Create a non-root user
RUN groupadd -r desktop && useradd -r -g desktop -G audio,video -s /bin/bash desktop \
    && mkdir -p /home/desktop && chown -R desktop:desktop /home/desktop

#----------------------
# Configure user, etc
WORKDIR /home/desktop

# Chrome policies: no password, autofill, translate or sign-in prompts
COPY chrome-policies.json /etc/opt/chrome/policies/managed/agent.json

USER desktop
# Default to bash
CMD ["bash"]