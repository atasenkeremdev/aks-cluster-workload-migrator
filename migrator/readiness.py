"""Controller readiness, distinct from application-level functional health."""


def evaluate(obj, mode):
    kind = obj.get('kind')
    spec, status = obj.get('spec', {}), obj.get('status', {})
    if kind == 'CronJob':
        return {'state': 'Prepared' if spec.get('suspend') else 'Applied',
                'detail': 'CronJob suspended' if spec.get('suspend') else 'CronJob enabled; job execution is not verified'}
    if kind not in ('Deployment', 'StatefulSet', 'DaemonSet'):
        return {'state': 'Applied', 'detail': 'API accepted resource; no workload health check for this kind'}
    generation = obj.get('metadata', {}).get('generation', 0)
    if status.get('observedGeneration', -1) < generation:
        return {'state': 'RollingOut', 'detail': 'Controller has not observed the current generation'}
    for condition in status.get('conditions', []) or []:
        if condition.get('type') == 'Progressing' and condition.get('status') == 'False':
            return {'state': 'Failed', 'detail': 'Controller reports rollout failure'}
    desired = status.get('desiredNumberScheduled', 0) if kind == 'DaemonSet' else spec.get('replicas', 1)
    ready = status.get('numberReady', 0) if kind == 'DaemonSet' else status.get('readyReplicas', 0)
    updated = status.get('updatedNumberScheduled', 0) if kind == 'DaemonSet' else status.get('updatedReplicas', 0)
    actual = status.get('currentNumberScheduled', 0) if kind == 'DaemonSet' else status.get('replicas', 0)
    if mode == 'prepare' and desired == 0:
        return {'state': 'Prepared' if actual == 0 else 'RollingOut',
                'detail': 'Passive workload has no replicas' if actual == 0 else 'Waiting for existing replicas to terminate'}
    if desired == 0:
        return {'state': 'Inactive', 'detail': 'No replicas requested; this does not verify a running application'}
    if kind == 'StatefulSet' and status.get('currentRevision') != status.get('updateRevision'):
        return {'state': 'RollingOut', 'detail': 'StatefulSet revisions differ'}
    available = status.get('numberAvailable', 0) if kind == 'DaemonSet' else status.get('availableReplicas', 0) if kind == 'Deployment' else ready
    if ready == desired and updated == desired and actual == desired and available == desired:
        return {'state': 'Ready', 'detail': 'Controller replicas are ready; application functional health is not verified'}
    return {'state': 'RollingOut', 'detail': f'Ready {ready}/{desired}; updated {updated}/{desired}'}
