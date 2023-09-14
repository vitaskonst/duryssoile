#!/bin/bash

HOST_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}"  )" &> /dev/null && pwd)

# Install Docker
wget -qO- https://get.docker.com/ | sh

# Install docker-compose
sudo curl -L "https://github.com/docker/compose/releases/download/1.27.4/docker-compose-$(uname -s)-$(uname -m)" -o /usr/local/bin/docker-compose
sudo chmod +x /usr/local/bin/docker-compose

# Install utility to dowload files from google drvie
pip install gdown

# Download the file with audios from google drive and unzip it
gdown --id 1plQkCdlCqty6IaMvz8JJ3UcfQ6wkR1VB
unzip audio.zip -d "$HOST_DIR"

# Setup application
sudo docker-compose -f "$HOST_DIR/docker-compose.yml" up --build -d
