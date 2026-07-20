# DPM — Deploy Project Manager

DPM is a small self-hosted control plane for deploying Git repositories as Docker Compose projects.

DPM does not implement its own process supervisor, component manifest, port allocator, healthcheck engine or static publisher. `compose.yml` is the only runtime source of truth.

## Responsibilities

DPM owns Git polling, deployment queue/history, Compose commands, project/service controls, deployment logs, container logs, Docker telemetry, web UI and CLI. Docker Compose owns images, containers, isolated networks, volumes, dependencies, healthchecks, runtime environment and ports.

## Installation

```bash
git clone https://github.com/DmitryLedentsov/dimka-project-manager.git
cd dimka-project-manager
sudo ./install.sh
```

The installer uses Docker's official Ubuntu/Debian repository, installs the Compose plugin, installs DPM under `/opt/deploy-project-manager`, creates `deploy-project-manager.service`, starts a shared Traefik proxy on port 80 and exposes DPM under `/admin`.

Default login: `admin / admin`.

Private Git repositories use `/var/lib/dpm/.ssh/id_ed25519`.

Update:

```bash
git pull origin master
sudo ./update.sh
```

The Compose-native update intentionally removes the old native service table and stops process groups recorded by previous DPM versions.

## Add a project

A repository needs a standard Compose file. No `dpm.yaml` is used.

```bash
dpm project add git@github.com:user/project.git --branch master --compose-file compose.yml --env-file /etc/project/compose.env
```

Environment-file auto-detection order:

```text
/etc/dpm/projects/<project>.env
/etc/<project>/compose.env
<repository>/.env
```

## Project lifecycle

Deploy performs Git checkout, Compose validation, image pull/build, `up -d --remove-orphans`, then waits for running/healthy services. A failed image build leaves the currently running stack untouched. Named volumes are never removed automatically.

DPM rejects managed Compose configurations containing automatic restart policies. A crashed container stays failed until an operator acts or a later deployment recreates it.

## Shared reverse proxy

DPM installs Traefik and creates the external `dpm-proxy` network. Projects opt into public routing with ordinary Traefik labels. Optional `dpm.role` and `dpm.url` labels improve UI display but do not define runtime behavior.

DPM itself remains a host systemd process on `127.0.0.1:8787`; Traefik routes `/admin` to it.

## CLI

```bash
dpm status
dpm project list
dpm project show 1
dpm project deploy 1
dpm project redeploy 1
dpm project start 1
dpm project stop 1
dpm project remove 1
dpm service list 1
dpm service start 1 api
dpm service stop 1 api
dpm service restart 1 api
dpm logs 1
dpm logs 1 api --follow
```

## Files

```text
/opt/deploy-project-manager
/etc/dpm/config.env
/etc/dpm/traefik-dynamic.yml
/var/lib/dpm/dpm.sqlite3
/var/lib/dpm/projects
/var/log/dpm
```
