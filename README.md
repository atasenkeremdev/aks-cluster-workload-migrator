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

The workspace follows three stages: **Select scope → Compare → Prepare /
Activate**. Live mode is selected initially. The sticky overview keeps source,
target, namespace count, action mode, connection and current activity visible.

1. Click **Load cluster contexts** and select actual source and target context names.
   A cluster's Azure name may differ from its kubeconfig context. Missing
   contexts must first be added with your `az aks get-credentials` procedure.
2. Click **Load source namespaces** and choose namespaces in the searchable
   multi-select. Select all includes entries hidden by search. Changing source
   clears the old list.
3. Open **Advanced settings** only when registry rewriting or offline JSON is
   needed. Enter full old/new registry hostnames to rewrite container images.
4. Start comparison. The scope folds after success. Progress and elapsed times
   stay visible while reading; detailed logs are collapsed by default.
5. Results align source and target resources in two columns. **Differences only**
   initially hides MATCH; turn it off or select MATCH status to inspect matches.
   Name/namespace search combines with namespace, kind and status filters.
   Resetting filters restores the differences-first view. Summary cards and
   downloads retain the full analysis.
6. Click either resource name for expected source-on-target (left) and current
   target (right). Changed fields appear red and Secret values are masked.
7. Choose **Prepare target** or **Activate** in stage three (also reachable
   through the mode button in the sticky overview). Eligible source actions use
   that mode and appear only for DIFF/SOURCE_ONLY. MATCH and target-only resources
   never get action buttons. Changing scope invalidates old actions until the
   comparison is rerun.

Offline mode is under **Advanced settings** and accepts JSON resources, arrays or
Kubernetes Lists. **Load example** provides a complete example. For pasted JSON,
click **Load namespaces from JSON** and choose the desired namespaces. Offline mode never syncs to Kubernetes.

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

Use the source resource action and review the explicit mode before confirming:

- **Prepare target / Prepare** (default): Deployment/StatefulSet replicas become 0; CronJobs
  become suspended. This can stop an existing target workload. CronJob suspend
  does not terminate already-running Jobs. ConfigMaps, Secrets and other
  configuration resources are still applied and can affect running consumers.
- **Activate / Activate**: apply the transformed source configuration, including its replica
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
exact target, mode and masked manifest. Updates default to server-side apply without force-conflicts. Creates and updates
use the same `aks-migrator` field manager. A five-minute, single-use preview token and
target identity/configuration checks prevent applying a stale preview. Status-only
controller changes can refresh resourceVersion; the actual write still uses an
atomic resourceVersion precondition. Target namespaces
must already exist. Controller-managed targets cannot be overwritten directly.

### Resolving activation conflicts

Field-ownership conflicts and stale resourceVersion conflicts are reported
separately. Ownership errors show the field-manager names when available;
raw kubectl stderr and credential values are never shown.

If **Activate** reports a field-ownership conflict, click **Preview guarded merge
update** (or select **Guarded merge update** under Existing target update method).
This is an explicit alternative, not an automatic retry with force-conflicts.
It previews a JSON merge patch that overwrites submitted fields, preserves omitted
target fields and replaces submitted lists in full. Review the affected resource
and confirm before executing. HPA, Terraform or GitOps may subsequently reconcile
those fields; coordinate ownership with their operators before cutover.

Controller-only status updates no longer invalidate otherwise unchanged previews.
Preview retries once after refreshing a version if only status changed. Changes
to desired configuration or object identity still block the operation. A write
rejected with a known conflict is recorded as **Conflict**, not **Unknown**.

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
Raw Secret manifests are **never** stored in the journal; Secret operations have
no automatic rollback. Guarded merge stages its input in a private temporary
file (owner-only on POSIX, inherited private-directory ACLs on Windows), then
removes it after kubectl returns, including errors and timeouts. Secret values
are kept out of process command arguments. Journals record source/target, mode, timestamps and write outcome.
A timeout after write submission is **Unknown** and requires target inspection.

**Preview rollback** restores the prior configuration for an updated resource or
requests deletion of a resource created by the operation. Review and explicitly
confirm. UID, desired-configuration fingerprint and resourceVersion guards prevent
reverting later user/configuration changes. Controller-only status updates do not
invalidate rollback. Updates use guarded replace; creates are undone with DELETE
UID/resourceVersion preconditions. DeletionRequested is not proof finalizers have
completed. RollbackApplied means the restore was accepted; check readiness again.
Rollback does not restore database/volume data, external effects or field ownership.

The stage-three result summary groups the latest journaled operation per target
resource into passive-ready, controller-ready, failed/review and pending states.
It reflects recorded checks, not a fresh cluster-wide health scan; use the history
controls to refresh readiness. Confirmation highlights the exact target and the
expected impact; technical manifests and log details stay expandable.

## Performance and limits

Reads batch supported kinds into one kubectl process per namespace, avoiding
repeated startup/auth overhead. API list calls remain sequential andsapshots
are non-atomic. Per-API timeout: 15s; subprocess budget: 25s per kind in the batch.
Up to 200 namespaces per analysis, four concurrent analysis jobs, and 100 latest
activity events. Results remain in memory; old completed jobs are cleaned on
later submissions after ten minutes. Browser disconnect does not cancel a job.

## Validation

```sh
python3 -m unittest discover -s tests -v
node --check web/app.js
```

Tests cover directional normalization, operator policy, preparation/HPA guards,
dependency checks, write conflicts, readiness, durablesapshots, unknown outcomes
and guarded rollback. Browser integration was exercised against simulated cluster
responses; raw DELETE transport was checked with real kubectl against a local
fake API. Real AKS writes and Windows execution require validation on your machine.


Optional browser regression tests require Playwright plus Chromium:

```sh
python3 tests/browser_workspace.py
```

The script uses a system Chromium when available, or Playwright's installed
Chromium. `BROWSER_EXECUTABLE` can point at a local browser binary. It covers
responsive layout, default difference filtering, search, stage navigation,
namespace selection, live progress and a simulated prepare/activate/rollback
workflow. It never writes to a real cluster.
