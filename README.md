# AKS Cluster Workload Migrator

Initial migration planner based on the aks-dev → nl-aks-dev shell workflow.
Python 3.12+; no third-party dependencies.

```sh
python3 -m unittest discover -s tests -v
python3 -m migrator.server --port 8081
```

Open the local server in your browser when running on your own machine. Click
**Load example**, then **Analyze migration** for a complete example. Context
fields are descriptive labels; this release does not connect to Kubernetes.
Paste Kubernetes **JSON** manifests (single object, array, or `kind: List`),
select namespaces, and optionally supply a literal registry replacement.
The report lists matches, differences, source-only and target-only resources,
with normalized source previews and JSON download. Secrets are compared in
memory and redacted in responses. Inputs are not persisted by the server.
Do not submit production secrets; ConfigMaps can also contain sensitive data.
The server binds to loopback and has no remote authentication layer.

## Shared policy

Supports Namespace, Deployment, StatefulSet, DaemonSet, CronJob, Service,
ConfigMap, Secret, ServiceAccount, Role, RoleBinding, PodDisruptionBudget,
HorizontalPodAutoscaler, NetworkPolicy and ActiveMQArtemis. Cluster-scoped AMQ
CRDs and RBAC are intentionally excluded from this initial planner.
Ingress remains under Terraform. Protected system namespaces, generated
resources, service account token secrets and the legacy PostgreSQL exclusion
are consistently excluded from both sides.

Normalization strips runtime metadata, preserves StatefulSet claim templates
and headless Services, removes OSM pod-template metadata and replaces literal
registry strings on both sides. This is structural comparison, not Kubernetes
semantic/defaulting equivalence. It does not validate API schemas, readiness,
RBAC, storage compatibility or dependency order. Secret representations must
use matching data/stringData encoding for meaningful comparison.

## Original roadmap

Read-only kubectl adapter with explicit context and API error handling; then
migration plans with dry-run validation, explicit execution approval and audit
results. Single-resource sync is now supported as described below; prune, email and
persistent-volume/database transfer remain outside scope. Existing production scripts have not been run.

## Live cluster comparison

Install a kubectl version compatible with your clusters (within one minor
version of each API server). Put it on PATH. On AKS with Entra authentication,
also install the kubeconfig's required `kubelogin`/Azure CLI plugin and sign in
on the machine running this application. Reuse a trusted existing kubeconfig;
never commit kubeconfig, tokens or certificates. No credentials are uploaded
through the UI. Kubeconfig exec plugins execute locally, so use trusted configs.

```sh
export KUBECONFIG=/path/to/existing/config
kubectl config get-contexts -o name
kubectl --context aks-dev --request-timeout=15s get namespaces
kubectl --context nl-aks-dev --request-timeout=15s get namespaces
python3 -m migrator.server --port 8081
```

Select **Live Kubernetes clusters**, load contexts, choose source and target,
load source namespaces, enter the comparison scope and analyze. The target
namespace may be absent; its source resources will be reported source-only.
The service never changes current-context and only issues config reads,
API discovery and resource GET/list operations. Reads include Secrets: use a
suitably scoped identity. Namespace listing and discovery access are required,
plus list access to supported resource kinds in the selected namespaces.
Any failed resource read aborts analysis rather than treating it as empty.
Unavailable API kinds are shown separately. Reads are sequential, non-atomic
snapshots and may take time across many namespaces. Per-command timeout is
25 seconds; API request timeout is 15 seconds. A maximum of 50 namespaces is
accepted per analysis.

For a private AKS endpoint, run on a machine with the required private DNS
and network access (for example the existing migration host). Keep the server
bound to loopback; an SSH tunnel can reach it from your workstation. It is
not an authenticated multi-user service. No migration or deletion is executed.

### Windows / PowerShell

Install Python 3.12+, Git and a compatible kubectl. If your AKS kubeconfig uses
an exec authentication plugin, install that plugin too (commonly kubelogin and
Azure CLI). Check `kubectl version --client` and authenticate using your
organization's existing AKS procedure. The application inherits your environment.

```powershell
Set-Location C:\path\to\aks-cluster-workload-migrator
# Optional: only needed when not using $HOME\.kube\config
$env:KUBECONFIG = "$HOME\.kube\config"
kubectl config get-contexts -o name
kubectl --context aks-dev --request-timeout=15s get namespaces
kubectl --context nl-aks-dev --request-timeout=15s get namespaces
py -3 -m unittest discover -s tests -v
py -3 -m migrator.server --port 8081
```

On your Windows computer, open `http://127.0.0.1:8081` in your browser. Keep
PowerShell open while using the app. Use Ctrl+C to stop. A corporate VPN may
be needed for private AKS DNS/endpoints. No cloud-hosted credentials are needed
when the application runs on Windows. This Windows procedure has not been
executed in the Linux development environment.


## Side-by-side comparison and individual sync (v0.3)

Load contexts and select exact names in the Source and Target dropdowns. The
application does not assume a target context named `nl-aks-dev` exists. If the
intended target is absent, use your established AKS credential setup procedure
to add it to your local kubeconfig, then reload contexts. Selecting another
context merely to bypass the error could point at the wrong cluster.

Click a resource name for normalized source (left) and target (right). Changed
JSON-pointer fields appear red, including masked Secret fields whose actual
values differ. The source preview includes configured registry/OSM transforms.

In live mode, source resources have **Sync to target** buttons. Each click
re-reads the source, prepares a transformed manifest and performs a target
server-side dry-run. Review the target context, namespace, operation and masked
manifest, then explicitly confirm. Updates use server-side apply with field
manager `aks-migrator`, without force-conflicts. New resources use create.
Target resourceVersion is retained for updates and checked before execution;
changes after preview require a new preview. Plans are single-use, expire in
five minutes and remain only in server memory. Restart discards pending plans.

Sync requires target write RBAC and a pre-existing target namespace. Namespace
creation, bulk sync, prune and volume contents are not included. Admission,
immutable fields and field ownership conflicts can prevent a sync; no automatic
forced override is attempted. Sync can trigger workload rollouts. Re-analyze
after successful sync to refresh results. These paths are tested with simulated
kubectl responses; live writes require validation on your own clusters.
