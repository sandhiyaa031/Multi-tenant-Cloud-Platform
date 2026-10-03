# Deploying DBPilot on k3s

**Status: these manifests have not yet been applied to a real cluster.** They
were written from the working Docker Compose stack and checked for valid YAML
only. Expect to adjust storage classes and image names.

## Why three nodes

| Node label | Runs | Why it is separate |
|---|---|---|
| `dbpilot.io/plane=control` | control database, API, collector, engine, web | must stay responsive whatever the other planes do |
| `dbpilot.io/plane=data` | primary, replica, PgBouncer | the workload being protected |
| `dbpilot.io/plane=twin` (tainted) | twin node agent, delayed standby, clones | replay is real load; the taint keeps everything else off this node |

On the single-machine Compose setup the planes share disk and the hypervisor's
CPU, and production latency visibly rises while a twin run is in progress.
Separate nodes are what removes that interference.

## Steps

1. Install k3s on three machines (one server, two agents) and confirm
   `kubectl get nodes` shows all three.
2. Label and taint:
   ```bash
   kubectl label node <control-node> dbpilot.io/plane=control
   kubectl label node <data-node>    dbpilot.io/plane=data
   kubectl label node <twin-node>    dbpilot.io/plane=twin
   kubectl taint node <twin-node>    dbpilot.io/plane=twin:NoSchedule
   ```
3. Provide a ReadWriteMany storage class named `nfs-client` (for example the
   NFS subdir external provisioner). The statement log volume is written on the
   data node and read on the other two.
4. Build and push the four images, then replace `REGISTRY/…:TAG` in `dbpilot.yaml`:
   ```bash
   docker build -t REGISTRY/dbpilot-controlplane:TAG -f controlplane/Dockerfile .
   docker build -t REGISTRY/dbpilot-twin:TAG         -f twin/Dockerfile .
   docker build -t REGISTRY/dbpilot-dataplane:TAG    dataplane/postgres
   docker build -t REGISTRY/dbpilot-pgbouncer:TAG    dataplane/pgbouncer
   docker build -t REGISTRY/dbpilot-web:TAG          web
   ```
5. Create the secret from your `.env` and apply:
   ```bash
   kubectl create namespace dbpilot
   kubectl -n dbpilot create secret generic dbpilot-secrets --from-env-file=.env
   kubectl apply -f deploy/k3s/dbpilot.yaml
   ```
6. Seed tenants and register them, as in the Compose flow, by running the seed
   SQL against `dp-primary` and `demo_seed.py` against the API.

## Differences from Compose

- CPU pinning is done by Kubernetes: equal requests and limits give Guaranteed
  QoS, and with the kubelet's static CPU manager policy, dedicated cores.
  `TWIN_INSTANCE_CPUS` is therefore not set.
- `TWIN_DELAY_S` is 900 (15 minutes), the value in the architecture
  specification; the development stack uses a shorter delay to iterate faster.
