# AKS Cluster Workload Migrator

A local UI for comparing AKS resources, preparing a passive target and explicitly
activating individual workloads. Based on the aks-dev → nl-aks-dev workflow.
Python 3.12+; no third-party Python dependencies. Default port: **8081**.

## Run on Windows

Install Python, Git, a kubectl version within one minor version of your AKS API
servers, and the authentication plugins required by your trusted kubeconfig
(commonly Azure CLI and kubelogin). Authenticate using your organization's AKS
procedure. Private endpoints may require corporate VPN/private DNS.

```powershell
Set-Location C:\path\to\aks-cluster-workload-migrator
# Optional: if using a non-default kubeconfig
$env:KUBECONFIG = "$HOME\.kube\config"
kubectl config get-contexts -o name
py -3 -m unittest discover -s tests -v
py -3 -m migrator.server --port 8081
```

Open `http://127.0.0.1:8081` on your Windows computer. Keep PowerShell open;
Ctrl+C stops the server. After updates, restart it and refresh with Ctrl+F5.
The server binds loopback, accepts same-origin JSON writes, and is intended for
one local operator. Do not expose it as an unauthenticated multi-user service.
Kubeconfig exec plugins run locally; use trusted kubeconfig files only.

On Linux, use `python3` in place of `py -3`. No package installation is needed.

## Compare

1. Choose **Live Kubernetes clusters** and **Load cluster contexts**.
2. Select actual source and target context names. A cluster's Azure name may
   differ from its kubeconfig context. Missing contexts must first be added
   with your established `az aks get-credentials` procedure.
3. Load source namespaces, then enter the desired namespaces, comma separated.
4. Optionally enter full old/new registry hostnames.
5. Analyze. Watch the activity log, request count and elapsed times.
6. Filter by namespace, kind and status. Filters combine locally; summary
   cards and report downloads retain the full analysis.
7. Click a resource for expected source-on-target (left) and current target
   (right). Changed JSON-pointer fields appear red; Secret values are masked.

Offline mode accepts JSON resources, arrays or Kubernetes Lists. **Load example**
provides a complete example. Offline mode never syncs to Kubernetes.

Source transformations are directional: target images/OSM labels remain visible
as differences. Registry replacement applies only to exact registry prefixes in
container/initContainer/ephemeralContainer image fields. ConfigMap and Secret
contents and AMQ custom-resource fields are not rewritten. There is no automatic
base64 credential conversion. Runtime metadata is ignored; headless Services
and StatefulSet claim templates are preserved. Kubernetes defaulting, list
ordering and API-version differences can still produce structural differences.

Controller-owned resources are excluded on both sides; migrate their owning
controller/custom resource instead. Ingress remains under Terraform. System
namespaces, generated resources, service-account tokens and the legacy
PostgreSQL exclusion are also skipped. AMQ CRDs, cluster RBAC, PVC/PV data,
StorageClasses, ingress/DNS cutover and database migration are not copied.

## Prepare, then activate

Click **Sync to target** and review the explicit mode before confirming:

- **Prepare** (default): Deployment/StatefulSet replicas become 0; CronJobs
  become suspended. This can stop an existing target workload. CronJob suspend
  does not terminate already-running Jobs. ConfigMaps, Secrets and other
  configuration resources are still applied and can affect running consumers.
- **Activate**: apply the transformed source configuration, including its replica
  count or CronJob suspend setting. Source workloads are not stopped. Arrange
  traffic, scheduler and queue-consumer cutover to avoid duplicate processing.

Passive mode is blocked for DaemonSet, ActiveMQArtemis and HPA because a generic
safe passive representation is not defined. It is also blocked when an existing
target HPA can scale the selected workload. Other reconcilers/GitOps tools must
be coordinated separately; preparation is not a whole-cluster quiescence guarantee.

Preflight checks required target references (ServiceAccounts, pull Secrets,
env/volume ConfigMaps and Secrets, PVCs and explicit StatefulSet StorageClasses)
for existence. It does not prove key contents, RBAC, storage compatibility,
Workload Identity, Key Vault access, node scheduling, image pull, network reachability
or application behavior. Complete those functional checks before cutover.

Each preview re-reads source and target and runs server-side dry-run. Review the
exact target, mode and masked manifest. Updates use server-side apply without
force-conflicts; creates use create. A five-minute, single-use preview token and
target resourceVersion checks prevent applying a stale preview. Target namespaces
must already exist. Controller-managed targets cannot be overwritten directly.

## Readiness and history

An accepted API write is **Applied**, not a completed migration. The UI checks
controller health every five seconds for about a minute. States include
**Prepared**, **RollingOut**, **Ready**, **Failed**, **Inactive** and **Applied**.
Use **Check readiness** in operation history to continue monitoring. Ready means
controller readiness, not a successful application-level functional test.
CronJob execution and AMQ operator health need separate verification.

Before a write, a durable record is stored in `.state/history.sqlite3` (gitignored).
`MIGRATOR_STATE_DIR` can select a different private directory. Backups include
raw pre-sync non-Secret manifests and may contain sensitive configuration.
POSIX files are created with owner-only permissions; Windows uses the directory's
inherited ACLs. Keep this directory private and use disk encryption as appropriate.
Raw Secret manifests are **never** persisted; Secret operations have no automatic
rollback. Journals record source/target, mode, timestamps and write outcome.
A timeout after write submission is **Unknown** and requires target inspection.

**Preview rollback** restores the prior configuration for an updated resource or
requests deletion of a resource created by the operation. Review and explicitly
confirm. UID, desired-configuration fingerprint and resourceVersion guards prevent
reverting later user/configuration changes. Controller-only status updates do not
invalidate rollback. Updates use guarded replace; creates are undone with DELETE
UID/resourceVersion preconditions. DeletionRequested is not proof finalizers have
completed. RollbackApplied means the restore was accepted; check readiness again.
Rollback does not restore database/volume data, external effects or field ownership.

## Performance and limits

Reads batch supported kinds into one kubectl process per namespace, avoiding
repeated startup/auth overhead. API list calls remain sequential and snapshots
are non-atomic. Per-API timeout: 15s; subprocess budget: 25s per kind in the batch.
Up to 50 namespaces per analysis, four concurrent analysis jobs, and 100 latest
activity events. Results remain in memory; old completed jobs are cleaned on
later submissions after ten minutes. Browser disconnect does not cancel a job.

## Validation

```sh
python3 -m unittest discover -s tests -v
node --check web/app.js
```

Tests cover directional normalization, operator policy, preparation/HPA guards,
dependency checks, write conflicts, readiness, durable snapshots, unknown outcomes
and guarded rollback. Browser integration was exercised against simulated cluster
responses; raw DELETE transport was checked with real kubectl against a local
fake API. Real AKS writes and Windows execution require validation on your machine.
