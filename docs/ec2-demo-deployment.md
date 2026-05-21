# Budget-Safe EC2 Demo Deployment

This deployment is for a public flagship demo on one EC2 instance. It uses Docker Compose and avoids RDS, ElastiCache, ALB, NAT Gateway, and ECS to keep costs controlled.

It runs Nginx, the IncidentOps frontend, Core API, Core worker, Postgres with pgvector, Redis, and the OpsIncident Collector daemon. Postgres, Redis, Core, Collector, and frontend are private to the Docker network. Only Nginx publishes ports `80` and `443`.

## Cost Notes

Use a small instance only for the demo window. A reasonable starting point is `t3.small` or `t3.medium` with 20-30 GiB gp3 EBS. Stop or terminate the instance when the demo is over. An idle EC2 instance, EBS volume, public IPv4 address, and snapshots can still cost money.

This stack is intentionally single-node and budget-first, not HA.

## EC2 Security Group

Inbound:

- `22/tcp` from your IP only
- `80/tcp` from the demo audience
- `443/tcp` from the demo audience

Do not expose Postgres, Redis, Core API, Collector health, or Next.js directly.

## Repository Layout

Clone the Core, Collector, and frontend repositories as siblings:

```bash
mkdir -p ~/incidentops
cd ~/incidentops
git clone https://github.com/Auro-rium/Ops-Incident-Core.git Ops-Incident-Core
git clone https://github.com/Auro-rium/OpsIncident-Collector.git Ops-Incident-Collector
git clone https://github.com/Auro-rium/incidentops-frontend.git Ops-Incident-frontend
cd Ops-Incident-Core
git checkout core
```

Expected layout:

```text
~/incidentops/
  Ops-Incident-Core/
  Ops-Incident-Collector/
  Ops-Incident-frontend/
```

The default Collector build context is `../Ops-Incident-Collector`. The default frontend build context is `../Ops-Incident-frontend`. Override them with `--collector-repo`, `--frontend-repo`, `COLLECTOR_REPO_PATH`, or `FRONTEND_REPO_PATH` if your paths differ.

## Bootstrap Instance

```bash
scripts/bootstrap_ec2.sh
```

If the script adds your user to the `docker` group, log out and back in before deploying.

## Configure Environment

```bash
cp deploy/ec2/.env.demo.example deploy/ec2/.env.demo
chmod 600 deploy/ec2/.env.demo
```

Edit:

```bash
PUBLIC_BASE_URL=http://YOUR_EC2_PUBLIC_DNS_OR_IP
FRONTEND_URL=http://YOUR_EC2_PUBLIC_DNS_OR_IP
CORS_ALLOW_ORIGINS=http://YOUR_EC2_PUBLIC_DNS_OR_IP
CORS_ORIGINS=http://YOUR_EC2_PUBLIC_DNS_OR_IP
BOOTSTRAP_ADMIN_EMAIL=admin@example.com
COLLECTOR_REPO_PATH=../Ops-Incident-Collector
FRONTEND_REPO_PATH=../Ops-Incident-frontend
```

`scripts/deploy_ec2_demo.sh` generates strong values for `POSTGRES_PASSWORD`, `JWT_SECRET`, and `BOOTSTRAP_ADMIN_PASSWORD`. The generated admin password stays in `deploy/ec2/.env.demo`, which is gitignored.

## Deploy

```bash
scripts/deploy_ec2_demo.sh \
  --public-url http://YOUR_EC2_PUBLIC_DNS_OR_IP \
  --collector-repo ../Ops-Incident-Collector \
  --frontend-repo ../Ops-Incident-frontend
```

The script prepares env files, generates a temporary self-signed HTTPS certificate if no cert exists, builds images, starts Postgres and Redis, runs `alembic upgrade head`, runs `python scripts/check_migrations.py`, starts Core/frontend/Nginx, bootstraps the admin, creates a demo project, writes `deploy/ec2/.env.runtime`, and starts the Collector daemon.

Core starts with:

```bash
APP_ENV=production
DB_CREATE_ALL=false
DB_REQUIRE_MIGRATIONS=true
LOCAL_INGEST_ENABLED=false
WORKER_MODE=queue
JOB_QUEUE_BACKEND=redis
RATE_LIMIT_BACKEND=redis
```

## TLS

The deploy script creates temporary self-signed files:

```text
deploy/ec2/nginx/certs/selfsigned.crt
deploy/ec2/nginx/certs/selfsigned.key
```

For a public demo, replace those files with a real certificate for your domain or put Cloudflare/Caddy/another TLS terminator in front of the instance.

## Verify

From the EC2 instance:

```bash
curl http://127.0.0.1/health
curl http://127.0.0.1/ready
curl http://127.0.0.1/api/v1/capabilities
docker compose --env-file deploy/ec2/.env.demo --env-file deploy/ec2/.env.runtime -f docker-compose.ec2-demo.yml exec -T collector \
  opsincident-collector daemon health --host 127.0.0.1 --port 8686
```

From your machine:

```bash
curl http://YOUR_EC2_PUBLIC_DNS_OR_IP/health
curl http://YOUR_EC2_PUBLIC_DNS_OR_IP/ready
```

Open `http://YOUR_EC2_PUBLIC_DNS_OR_IP` and sign in with the admin email and generated password in `deploy/ec2/.env.demo`.

## Smoke Test

```bash
scripts/smoke_ec2_demo.sh
```

The smoke script checks `/health`, `/ready`, capabilities, Collector `core_reachable`, forced Collector fixture sync, Core search evidence, Core investigation citations, and workflow run creation.

Use a public API base if testing from outside the instance:

```bash
API_BASE_URL=http://YOUR_EC2_PUBLIC_DNS_OR_IP/api scripts/smoke_ec2_demo.sh
```

## Backup

```bash
scripts/backup_db.sh
```

Backups are written to `deploy/ec2/backups/`. Copy backups off the instance before terminating it.

## Teardown

Stop containers while keeping data volumes:

```bash
scripts/teardown_ec2_demo.sh
```

Stop containers and remove Docker volumes:

```bash
scripts/teardown_ec2_demo.sh --volumes
```

After the demo, stop or terminate the EC2 instance to control costs.

For the current demo instance:

```bash
aws ec2 stop-instances --region us-east-1 --instance-ids i-083255401a6e27271
```

## Useful Commands

```bash
docker compose --env-file deploy/ec2/.env.demo --env-file deploy/ec2/.env.runtime -f docker-compose.ec2-demo.yml ps
docker compose --env-file deploy/ec2/.env.demo --env-file deploy/ec2/.env.runtime -f docker-compose.ec2-demo.yml logs -f api
docker compose --env-file deploy/ec2/.env.demo --env-file deploy/ec2/.env.runtime -f docker-compose.ec2-demo.yml logs -f collector
```

## Manual GitHub Actions Redeploy

The Core repo includes `.github/workflows/deploy-ec2-demo.yml` for manual redeploys. It SSHes into the EC2 host, pulls all three sibling repos, runs the EC2 deploy script with the Collector and frontend repo paths, then runs `scripts/smoke_ec2_demo.sh`.

Required GitHub secrets:

- `EC2_HOST`: `44.200.229.227` or the EC2 public DNS
- `EC2_USER`: `ubuntu`
- `EC2_SSH_KEY`: private key for SSH access to the instance

Manual redeploy command executed by the workflow:

```bash
scripts/deploy_ec2_demo.sh \
  --public-url http://44.200.229.227 \
  --collector-repo ../Ops-Incident-Collector \
  --frontend-repo ../Ops-Incident-frontend
```

## Caveats

- This is a demo stack, not a production HA deployment.
- The Collector uses a JWT written to `deploy/ec2/.env.runtime`; rerun `scripts/deploy_ec2_demo.sh` to refresh it.
- Local path ingest is disabled. The Collector is the ingestion path.
- Postgres and Redis data live in Docker volumes on the EC2 instance.
- The bundled fixture is intentionally small and safe. Replace the Collector source path only with data you are authorized to sync.
