# Deployment: what DBPilot needs, what was tested, and what to do for the review

## The short answer

**Run it locally on the review machine.** That is the dependable option: it is the configuration every test and every
recorded result in this repository was produced on, it needs no account and no payment, and nothing is exposed beyond that
computer. A hosted copy is *possible* on a machine with enough memory and disk, but **no hosted deployment was created or
tested for this project**, and a free host that can run all of it reliably is not a given (below). Deploying only the
frontend would not deploy DBPilot.

| | State |
|---|---|
| Local deployment with Docker Compose | **Works.** Tested on the development laptop; this is what the browser tests run against |
| Local deployment on a smaller machine (`docker-compose.small.yml`) | Configuration validated (`docker compose config` shows no core pinning); **not run** on a small machine |
| Any hosted deployment (VM, PaaS, Codespaces) | **Not done.** No paid or free resource was created, and nothing is publicly reachable |
| Kubernetes (`deploy/k3s/`) | Manifests written in an earlier phase; never deployed; not the review path |

## What "the whole project" is

A browser talking to one web server is the smallest part. The system is nine long-running services and a data volume each:

| Service | Image / source | Needs | Measured now |
|---|---|---|---|
| `web` | nginx serving the built React app, proxying `/api/` | 128 MB | 19 MiB |
| `api` | FastAPI (`controlplane/`) | 384 MB | 60 MiB |
| `control-db` | PostgreSQL 18, DBPilot's own database | 512 MB, 0.3 GB disk | 84 MiB |
| `collector` | polls the data plane, writes telemetry | 384 MB | 44 MiB |
| `engine` | verifies proposals, runs canaries | 384 MB | 42 MiB |
| `dp-primary` | the PostgreSQL being tuned (with `pg_stat_statements`, HypoPG) | 1.5 GB (laptop profile) | 1.1 GiB |
| `dp-replica` | streaming replica | 768 MB | 393 MiB |
| `pgbouncer` | the pooler tenants connect through | 128 MB | 3 MiB |
| `twin` | the digital twin: a delayed standby plus clones it starts and stops | 1.5 GB | 270 MiB idle, more during a run |
| `demo-load` | the demo workload generator (optional) | 384 MB | 28 MiB |

Memory limits add up to about **5.8 GB**; measured use at rest is about **2.5 GB**, and it rises while the twin runs two
clones. **Disk is the larger surprise:** after weeks of testing the volumes hold about 20 GB (the twin's copies of the data
directory alone are 10.6 GB, the primary 4.5 GB, the replica 4.5 GB). A fresh start is much smaller, but plan for at least
**15 GB free** and the images (about 6 GB).

**CPU.** `docker-compose.yml` pins services to named cores 0 to 19 so that the twin's clones do not compete with the primary.
On a host with fewer cores Docker refuses to start those containers. Use `docker-compose.small.yml` there, which removes the
pinning. Without pinning, latencies are noisier and the digital twin's intervals are wider, so such a host shows the system
working and says little about its measurements.

## Why deploying "just the frontend" is not a deployment

A static host (GitHub Pages, Netlify, Vercel, Cloudflare Pages) can serve `web/dist`. But the console is a client of the
API: every page reads from it. Without the API, the control-plane database, the collector (which reads a real PostgreSQL),
the engine and the twin, the pages would be empty or would show errors, which would be a worse demonstration than the
real thing running locally. Hosting the pages elsewhere and keeping the rest on a laptop would also expose that laptop's API
to the internet, which is the opposite of what the review needs.

## Options considered

| Option | Can it run all ten services? | Cost | Verdict |
|---|---|---|---|
| **The review laptop, local Docker** | Yes (done) | none | **Use this** |
| Another computer you own (Linux, 16 GB, SSD) | Yes, with `docker-compose.small.yml` if it has under 20 threads | none | Reasonable backup. Untested here |
| Free static hosting for the frontend | No: frontend only | none | Not a deployment of DBPilot |
| Free application platforms (Render, Fly.io, Railway and similar) | No, as far as their model goes: they run one or a few web containers with small memory, not a primary, a replica and a twin that copy data directories, and their free databases are single instances | none or card required | Do not rely on it. Check their current terms if you want to try |
| Free-tier small cloud VMs (about 1 GB of RAM) | No: the stack needs several times that | none | Too small |
| A free cloud VM with a large always-free allowance | Memory would be enough on the largest such offers; disk and arm64 images are unchecked | needs an account, usually with a card on file, which I did not create | Possible; needs your decision. Not tested |
| GitHub Codespaces | Plausibly, on a 4-core / 16 GB codespace using `docker-compose.small.yml`; the free monthly allowance is limited, and the first start is likely to take 10 minutes or more | free allowance, then billing | A private, short-lived copy is feasible; **not tested**. Never make its ports public |

I did not create any of these. If you want a hosted copy, the decision to create an account or use quota is yours; the steps
for the one zero-cost candidate are at the end.

## The review-day plan: local, and secured

Everything below is already true of the repository; nothing needs to be changed to be safe on one computer.

- **Nothing is reachable from outside the machine.** Every published port in `docker-compose.yml` is bound to `127.0.0.1`
  (web 5173, API 8000, control database 5440, primary 5441, replica 5442, pooler 6432, twin 8090). Other computers on the
  network cannot connect to any of them.
- **Authentication.** Argon2id password hashes; signed 8-hour tokens; sign-in is rate-limited to 8 failures per 5 minutes per
  address; a role is checked on every request against the database, so removing a member takes effect at once.
- **Secrets** live in `.env`, which is git-ignored. `scripts/make_env.sh` writes one with random values for a new machine and
  refuses to overwrite an existing file. No secret is in the repository (`.env.example` holds placeholders only).
- **Sign-up** is open by default so that anyone can create their own organization. On any machine other people can reach,
  set `SIGNUP_ENABLED=false` in `.env`.
- **No TLS.** The console is served over plain HTTP on `localhost`, which is fine on one computer and not on a network. If it
  is ever reachable by others, put it behind a TLS-terminating reverse proxy first.
- **The LLM key**, if any, is read from `.env` by the API container only; the browser never sees it, and `GET /agent/status`
  reports the provider and model, never the key.

### Commands

First time on this machine (about 10 minutes, mostly building images and loading the demo data):

```bash
git clone https://github.com/sandhiyaa031/Multi-tenant-Cloud-Platform.git
cd Multi-tenant-Cloud-Platform
scripts/make_env.sh            # only if there is no .env; prints the administrator's password once
scripts/review_start.sh        # builds, seeds, starts demo traffic, and waits until the console has data
```

Every other time (a minute or two):

```bash
scripts/review_start.sh
```

Is it ready? (read-only; exits 0 only when the demonstration can start from a clean state)

```bash
python scripts/demo_admin.py status
```

When finished:

```bash
scripts/review_stop.sh          # undoes anything a demonstration left behind, then stops everything and keeps the data
```

Removing the data as well (the next start rebuilds and reseeds everything):

```bash
docker compose --profile demo down -v
```

On a machine with fewer than about 20 threads:

```bash
SMALL=1 scripts/review_start.sh
```

### Before the review

| When | Do |
|---|---|
| The day before | `scripts/review_start.sh`, then rehearse the guided demo once. Leave the stack running or `review_stop.sh` |
| An hour before | Plug the laptop in. Close other heavy programs: the machine's speed varies with load, and the twin's measurements with it. Run `python scripts/demo_admin.py status` |
| For the twin to be shown live | Start the verification about 20 minutes before you need the verdict (8 replay pairs take about 17 minutes on this laptop), or open a proposal that already has a stored twin result: both are real |
| If something misbehaves | `python scripts/demo_admin.py reset` restores the objective and rolls back anything left applied. Failing that, `docker compose logs --tail 50 engine` |
| If Docker or the machine fails | Fall back to the recorded evidence: `docs/evidence/`, the screenshots under `docs/img/`, and `docs/REPORT.md`. These are the same numbers, not a different story |

## If you decide to host a private copy (Codespaces; untested)

This needs your GitHub account and uses your free allowance, so I have not done it. The steps, for you to try only if you want a
hosted copy:

1. On the repository's GitHub page choose Code, Codespaces, new codespace on `main`, with the 4-core machine type.
2. In the terminal: `scripts/make_env.sh`, then `SMALL=1 scripts/review_start.sh`. Expect 10 to 20 minutes the first time.
3. Open the forwarded port 5173. Leave its visibility **Private**: Public would let anyone with the link reach a console
   that has an administrator account and an open sign-up.
4. Stop the codespace afterwards; its disk and its secrets stay with it until deleted.

Limits you should expect: the free allowance is limited and set by GitHub (check your account's current figure before
relying on it); a stopped codespace's data may be deleted after a retention period GitHub decides; the digital twin's
measurements will be noisier than on a dedicated laptop; and it will be slower to start than your own machine. For these reasons it is a convenience, not a replacement for the local run.

## What this does not claim

- That the stack has run on any machine other than the development laptop.
- That `docker-compose.small.yml` produces a healthy stack on a small host: only that it removes the core pinning that would
  make Docker refuse to start it.
- That any free host will run it reliably. The numbers above are what it needs; whether a given offer provides them, at what
  current terms, is for you to check.
