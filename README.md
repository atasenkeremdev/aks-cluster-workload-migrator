# AKS Cluster Workload Migrator

Initial migration planner based on the aks-dev → nl-aks-dev shell workflow.
Python 3.12+; no third-party dependencies.

```sh
python3 -m unittest discover -s tests -v
python3 -m migrator.server --port 8080
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

## Next stages

Read-only kubectl adapter with explicit context and API error handling; then
migration plans with dry-run validation, explicit execution approval and audit
results. No apply, prune, email or persistent-volume/database transfer is
implemented in this release. Existing production scripts have not been run.
