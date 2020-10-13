#!/usr/bin/env bash

set -ex

docker-compose up -d elasticsearch
docker-compose exec elasticsearch chmod 777 ./data

docker-compose up -d seqr
docker-compose logs postgres
docker-compose logs elasticsearch
docker-compose logs redis
docker-compose exec seqr curl elasticsearch:9200
sleep 30
docker-compose logs seqr
echo -ne 'testpassword\n' docker-compose exec seqr python manage.py createsuperuser --username test --email test@test.com
