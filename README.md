# AKS Cluster Workload Migrator

A local UI for comparing AKS resources, preparing a passive target and explicitly
activating individual workloads. Use a source-cluster → target-cluster workflow.
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

### Namespace exclusion policy

At startup the server reads `excluded-namespaces.txt` from the repository root,
independently of the current working directory. Use one exact Kubernetes namespace
name per line; blank lines, duplicate entries and `#` comments are supported.
Wildcards are not supported. Restart the server after editing the file.
A missing, unreadable or invalid file prevents startup rather than silently
allowing migration. Built-in system namespace protections always remain active.

To select a different policy file:

```sh
python3 -m migrator.server --exclusions-file /path/to/excluded-namespaces.txt --port 8081
```

File and system policy exclusions appear as disabled entries when present in the
loaded namespace list. The server rejects excluded namespaces for offline/live
analysis and sync previews, including direct API requests. They cannot be
re-enabled in the UI. Add session-specific names in **Additional exclusions**
(comma or whitespace separated), or uncheck namespaces in the picker. Additional
UI exclusions disable their picker entries and remove them from the selected
scope; removing a name makes it selectable again without selecting it automatically.
These additions last only until the page is reloaded and do not edit the file.
Changed selections invalidate previous comparisons and require analysis again.
Select all skips disabled entries. Reports include exclusion names and reasons.

### Troubleshooting namespace loading (HTTP 502)

**Load cluster contexts** reads the local kubeconfig; success does not confirm
cluster connectivity. **Load source namespaces** contacts the selected cluster's
API and requires permission to list namespaces. A 502 can indicate a failed
kubectl request; read the accompanying UI message for the cause.

In the same PowerShell window used to start the application, run:

```powershell
kubectl config get-contexts -o name
kubectl --context "YOUR_SOURCE_CONTEXT" get namespaces --request-timeout=15s
```

Replace `YOUR_SOURCE_CONTEXT` with the exact source context selected in the UI.
This lists namespaces in the selected cluster; it does not change the current
context or cluster resources.

If kubectl fails, check the reported cause: corporate VPN/private DNS and API
network access, trusted kubeconfig/TLS settings, authentication, or RBAC permission
to list namespaces. For Azure authentication errors, refresh your Azure CLI login
using your organization's procedure and verify that kubelogin is installed if
your kubeconfig requires it.

If kubectl succeeds but the UI still fails, stop the existing application with
**Ctrl+C** and restart it from that same working PowerShell window:

```powershell
Get-Command kubectl | Select-Object Source
$env:KUBECONFIG
py -3 -m migrator.server --port 8081
```

An empty `KUBECONFIG` value is normal: kubectl uses the default
`$HOME\.kube\config`. The application inherits its environment at startup, so
restart it after changing PATH, KUBECONFIG, or other environment settings.
Restarting from the working terminal resolved a reported case; the exact original
cause was not confirmed.

Open `http://127.0.0.1:8081`, press **Ctrl+F5**, reload cluster contexts, select
the exact context that worked in kubectl, and retry **Load source namespaces**.
If it still fails, compare the UI error with kubectl's error and note whether
the failure is consistent or intermittent. When reporting errors, remove tokens,
credentials and sensitive addresses; do not share kubeconfig contents.

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
   Uncheck namespaces to exclude them from comparison and migration actions. The
   overview shows included/excluded counts, and **Excluded namespaces** remains
   visible when the scope panel is collapsed. Expand it to see names and reasons:
   **User selection** for unchecked namespaces, **File policy** for file exclusions and **System policy** for protected
   namespaces returned by the cluster or found in offline JSON. Select an unchecked
   namespace again to include it; protected namespaces cannot be selected. Search
   only filters the picker display and does not change the scope. Changing the
   selection invalidates the previous comparison; run comparison again before
   applying changes. Downloaded reports include the analyzed namespace scope.
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
7. Choose an action beside the source resource. Configuration uses **Sync to
   target**. Workloads offer **Prepare target** and **Activate target**; DaemonSet
   and AMQ offer activation only because passive preparation is unsupported.
   Actions appear only for DIFF/SOURCE_ONLY. MATCH and target-only resources
   never get target sync action buttons. MATCH Deployments and StatefulSets may
   still offer **Scale source to zero**. Changing scope invalidates old actions until the
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

There is no global action-mode selector. Choose the action beside a resource; a
single review dialog shows its destination, expected effect and validation status.
Confirmation stays disabled until server-side validation succeeds. Cancelling
during validation cannot trigger a later write.

- **Sync to target**: copy configuration such as Secrets, ConfigMaps, Services,
  RBAC and HPA without presenting it as workload activation. HPA may immediately
  resize target workloads; Secret backups are not stored.


- **Prepare target / Prepare** (default): Deployment/StatefulSet replicas become 0; CronJobs
  become suspended. This can stop an existing target workload. CronJob suspend
  does not terminate already-running Jobs. ConfigMaps, Secrets and other
  configuration resources are still applied and can affect running consumers.
- **Activate target / Activate**: apply the transformed source configuration, including its replica
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

### Scale source workloads to zero

After comparing live clusters, choose **Scale source to zero** beside a source
Deployment or StatefulSet. This action also supports MATCH resources; turn off
**Differences only** to show them. It is unavailable for offline manifests,
target-only resources, workloads already requesting zero replicas, excluded
namespaces, or stale comparisons. CronJobs, DaemonSets and custom resources do
not use this action.

The review explicitly identifies the **SOURCE** context, namespace and workload.
It re-reads the source and checks migration exclusions, controller ownership and
source HPAs, then runs a server-side dry-run. An HPA controlling the workload
blocks the action. Confirming patches only the source `scale` subresource to
`spec.replicas: 0`; no target resource is written and no source template or image
is copied or changed. This requires RBAC read access to the workload and HPAs,
and patch access to the workload's `scale` subresource. Use a kubectl version
compatible with the cluster that supports `patch --subresource=scale`.

**This stops source pods and may interrupt traffic or processing.** Verify target
readiness and coordinate traffic and consumer cutover before confirming. This is
an explicit action per workload; there is no automatic shutdown after activation
or bulk source shutdown. A five-minute, single-use preview, source UID/configuration
checks and an atomic resourceVersion guard block stale writes. HPAs are checked
again at execution, but another controller or a newly created autoscaler can still
reactivate the source; coordinate GitOps and autoscaling separately.

Cancellation performs no write. Every attempted write is journaled before the
request, with its source role and previous replica count. History marks acceptance
as **Applied**, then **Check readiness** waits for the source controller to observe
zero replicas and reports **ScaledDown**. This is a controller check, not proof
that traffic, all terminating pods or external processing have stopped. Source
operations do not count toward target readiness totals. Conflicts and uncertain
outcomes require inspection of the source before retrying. Automatic rollback is
not offered for source scaling; recover manually using the recorded previous
replica count after reviewing current state. Run comparison again after a write.

### Bulk actions

The comparison toolbar shows **Sync all (N)** when at least two eligible
configuration resources are in the current filtered results, and **Activate all
(N)** when at least two eligible workloads are present. Search, namespace, kind
and status filters all limit the batch. Matching, target-only, excluded and stale
resources are never included. Each batch supports up to 100 resources; narrow
filters for larger results.

The same review dialog lists every resource and its validation state. All items
must pass server-side dry-run before confirmation is enabled. Ownership conflicts
can be reviewed with guarded updates for the whole listed batch, with a fresh
validation. Cancellation discards previews and never executes writes. Expired
previews require validation again.

Execution is sequential and stops at the first failure. It is **not atomic**:
earlier successful writes remain applied, later items are marked not processed,
and individual operations/backups remain in history. API acceptance is not
readiness; use operation history to check health. After any execution attempt,
run analysis again before applying further changes. Closing the browser during
execution can interrupt the remaining client-driven batch; inspect history and
re-analyze rather than assuming everything was applied.

### Resolving activation conflicts

Field-ownership conflicts and stale resourceVersion conflicts are reported
separately. Ownership errors show the field-manager names when available;
raw kubectl stderr and credential values are never shown.

If **Activate** reports a field-ownership conflict, click **Review guarded update** in the same review dialog. The underlying
update method is available under **Advanced options and technical details**.
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

The separate results/history section groups the latest journaled operation per target
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
namespace selection, live progress and simulated configuration-sync and prepare/activate/rollback workflows,
including inline ownership-conflict recovery and cancellation during preview. It never writes to a real cluster.
